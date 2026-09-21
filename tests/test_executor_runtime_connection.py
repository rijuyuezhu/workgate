import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue

from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.profile import (
    ExecutorAlreadyRunningError,
    ExecutorProfile,
    executor_run_lock,
)
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.executor import (
    SESSION_CHANGE_CWD_OP,
    SESSION_CREATE_OP,
    ExecutorCommand,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
    JobInventorySummary,
    SessionInventorySummary,
    ShellInventorySummary,
)
from workgate.protocol.ids import new_command_id, new_executor_id


class _FakeControlClient:
    profiles: list[ExecutorProfile] = []
    hellos: list[ExecutorHelloRequest] = []
    hello_seen: asyncio.Event | None = None

    def __init__(self, profile: ExecutorProfile) -> None:
        self.profile = profile
        self.closed = False
        type(self).profiles.append(profile)

    async def hello(
        self, message: ExecutorHelloRequest
    ) -> ExecutorHelloResponse:
        type(self).hellos.append(message)
        hello_seen = type(self).hello_seen
        if hello_seen is not None:
            hello_seen.set()
        return ExecutorHelloResponse(
            heartbeat_interval_s=30,
            offline_after_s=90,
            poll_timeout_s=25,
        )

    async def heartbeat(self) -> None:
        return None

    async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
        await asyncio.Event().wait()
        return None

    async def submit_result(self, result: ExecutorResult) -> None:
        raise AssertionError("no command result expected")

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_client() -> None:
    _FakeControlClient.profiles = []
    _FakeControlClient.hellos = []
    _FakeControlClient.hello_seen = None


@pytest.mark.asyncio
async def test_executor_runtime_without_final_profile_stays_in_migration_mode(
    tmp_path: Path,
) -> None:
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                workspace_root=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        )
    )

    await runtime.start()
    try:
        assert runtime.connection is None
        await runtime.start()
        with executor_run_lock(runtime.services.state_store):
            pass
    finally:
        await runtime.aclose()

    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await runtime.start()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "session_id", "args", "message"),
    [
        (
            "ui.dashboard.snapshot",
            "sess_0000000000000000000001",
            {},
            "must not carry",
        ),
        ("ui.files.list", "sess_0000000000000000000001", {}, "must not carry"),
        (
            "ui.terminals.list",
            "sess_0000000000000000000001",
            {},
            "must not carry",
        ),
        (SESSION_CREATE_OP, None, {"workdir": "."}, "requires session_id"),
        (
            SESSION_CREATE_OP,
            "sess_0000000000000000000001",
            {"workdir": ""},
            "requires workdir",
        ),
        (
            SESSION_CREATE_OP,
            "sess_0000000000000000000001",
            {"workdir": ".", "label": 7},
            "label must be a string",
        ),
        (
            SESSION_CHANGE_CWD_OP,
            "sess_0000000000000000000001",
            {"workdir": ""},
            "requires workdir",
        ),
    ],
)
async def test_executor_runtime_protocol_guards_fail_closed(
    tmp_path: Path,
    op: str,
    session_id: str | None,
    args: dict[str, JsonValue],
    message: str,
) -> None:
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                workspace_root=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        )
    )
    command = ExecutorCommand(
        id=new_command_id(), op=op, session_id=session_id, args=args
    )

    with pytest.raises(ValueError, match=message):
        await runtime._execute_protocol_command(command)


@pytest.mark.asyncio
async def test_executor_runtime_profile_starts_v1_loop_and_holds_profile_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.executor import control_client as control_client_module

    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(workspace_root=workspace, state_dir=state_dir)
        )
    )
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    assert runtime.profile_store is not None
    runtime.profile_store.save(profile)
    _FakeControlClient.hello_seen = asyncio.Event()
    monkeypatch.setattr(
        control_client_module, "ExecutorControlClient", _FakeControlClient
    )

    await runtime.start()
    try:
        assert runtime.connection is not None
        await asyncio.wait_for(
            _FakeControlClient.hello_seen.wait(), timeout=0.5
        )
        assert _FakeControlClient.profiles == [profile]
        assert len(_FakeControlClient.hellos) == 1
        hello = _FakeControlClient.hellos[0]
        assert hello.workspace_root == str(workspace.resolve(strict=False))
        assert hello.sessions == ()
        assert hello.shells == ()
        assert hello.jobs == ()
        with (
            pytest.raises(ExecutorAlreadyRunningError),
            executor_run_lock(runtime.services.state_store),
        ):
            raise AssertionError(
                "duplicate executor loop acquired profile lock"
            )
    finally:
        await runtime.aclose()

    with executor_run_lock(runtime.services.state_store):
        pass


@pytest.mark.asyncio
async def test_executor_runtime_hello_includes_resource_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.executor import control_client as control_client_module

    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(workspace_root=workspace, state_dir=state_dir)
        )
    )
    session_id = "sess_0000000000000000000001"
    before = SessionInventorySummary(
        session_id=session_id,
        resolved_workdir=str(workspace),
        has_persistent_shells=True,
        has_active_jobs=True,
    )
    after = before.model_copy(update={"has_active_jobs": False})
    session_snapshots = iter(((before,), (after,)))
    monkeypatch.setattr(
        runtime.sessions, "inventory", lambda: next(session_snapshots)
    )
    shell = ShellInventorySummary(shell_id="shell-1", session_id=session_id)
    job = JobInventorySummary(
        job_id="job-1", session_id=session_id, status="lost"
    )

    async def reconnect_inventory(
        session_ids: frozenset[str],
    ) -> tuple[
        tuple[ShellInventorySummary, ...],
        tuple[JobInventorySummary, ...],
    ]:
        assert session_ids == frozenset({session_id})
        return (shell,), (job,)

    monkeypatch.setattr(
        runtime.shell, "reconnect_inventory", reconnect_inventory
    )
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    assert runtime.profile_store is not None
    runtime.profile_store.save(profile)
    _FakeControlClient.hello_seen = asyncio.Event()
    monkeypatch.setattr(
        control_client_module, "ExecutorControlClient", _FakeControlClient
    )

    await runtime.start()
    try:
        await asyncio.wait_for(
            _FakeControlClient.hello_seen.wait(), timeout=0.5
        )
        hello = _FakeControlClient.hellos[-1]
        assert hello.sessions == (after,)
        assert hello.shells == (shell,)
        assert hello.jobs == (job,)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_reconnect_hello_retries_when_session_ids_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                workspace_root=workspace,
                state_dir=tmp_path / "state",
            )
        )
    )
    first_id = "sess_0000000000000000000001"
    second_id = "sess_0000000000000000000002"
    first = SessionInventorySummary(
        session_id=first_id,
        resolved_workdir=str(workspace),
    )
    second = SessionInventorySummary(
        session_id=second_id,
        resolved_workdir=str(workspace),
    )
    snapshots = iter(((first,), (second,), (second,), (second,)))
    monkeypatch.setattr(runtime.sessions, "inventory", lambda: next(snapshots))
    observed: list[frozenset[str]] = []

    async def reconnect_inventory(
        session_ids: frozenset[str],
    ) -> tuple[
        tuple[ShellInventorySummary, ...],
        tuple[JobInventorySummary, ...],
    ]:
        observed.append(session_ids)
        return (), ()

    monkeypatch.setattr(
        runtime.shell, "reconnect_inventory", reconnect_inventory
    )

    hello = await runtime._build_reconnect_hello()

    assert observed == [
        frozenset({first_id}),
        frozenset({second_id}),
    ]
    assert hello.sessions == (second,)


@pytest.mark.asyncio
async def test_executor_runtime_terminal_attach_owns_stream_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.executor.terminal import stream as stream_module

    settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
    )
    runtime = build_executor_runtime(resolve_executor_config(settings))
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    assert runtime.profile_store is not None
    runtime.profile_store.save(profile)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeStream:
        stream_id = "stream_abcdefghijklmnopqrstuvwxyz"
        shell_id = "shell-1"
        backend = "fake-pty"

        async def run(self) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def connect_stream(
        current_profile: ExecutorProfile,
        *,
        stream_id: str,
        shell_id: str,
        cols: int,
        rows: int,
    ) -> FakeStream:
        assert current_profile == profile
        assert stream_id == FakeStream.stream_id
        assert shell_id == FakeStream.shell_id
        assert (cols, rows) == (101, 37)
        return FakeStream()

    monkeypatch.setattr(
        stream_module, "connect_executor_terminal_stream", connect_stream
    )

    result = await runtime._execute_protocol_command(
        ExecutorCommand(
            id=new_command_id(),
            op="terminal.attach",
            args={
                "stream_id": FakeStream.stream_id,
                "shell_id": FakeStream.shell_id,
                "cols": 101,
                "rows": 37,
            },
        )
    )

    assert result == {
        "stream_id": FakeStream.stream_id,
        "shell_id": FakeStream.shell_id,
        "backend": "fake-pty",
        "connected": True,
    }
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await runtime.aclose()
    await asyncio.wait_for(cancelled.wait(), timeout=0.5)


@pytest.mark.asyncio
async def test_executor_runtime_restart_reuses_same_profile_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.executor import control_client as control_client_module

    settings = Settings(
        workspace_root=tmp_path / "workspace", state_dir=tmp_path / "state"
    )
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    seed = build_executor_runtime(resolve_executor_config(settings))
    assert seed.profile_store is not None
    seed.profile_store.save(profile)
    monkeypatch.setattr(
        control_client_module, "ExecutorControlClient", _FakeControlClient
    )

    for _ in range(2):
        _FakeControlClient.hello_seen = asyncio.Event()
        runtime = build_executor_runtime(resolve_executor_config(settings))
        await runtime.start()
        try:
            await asyncio.wait_for(
                _FakeControlClient.hello_seen.wait(), timeout=0.5
            )
        finally:
            await runtime.aclose()

    assert [item.executor_id for item in _FakeControlClient.profiles] == [
        profile.executor_id,
        profile.executor_id,
    ]
    assert [item.credential for item in _FakeControlClient.profiles] == [
        profile.credential,
        profile.credential,
    ]
