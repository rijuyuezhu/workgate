import asyncio
from pathlib import Path

import pytest

from workgate.config.settings import Settings
from workgate.executor.profile import (
    ExecutorAlreadyRunningError,
    ExecutorProfile,
    executor_run_lock,
)
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.executor import (
    ExecutorCommand,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
)
from workgate.protocol.ids import new_executor_id


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
        Settings(
            workspace_root=tmp_path / "workspace", state_dir=tmp_path / "state"
        )
    )

    await runtime.start()
    try:
        assert runtime.connection is None
        with executor_run_lock(runtime.services.state_store):
            pass
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_executor_runtime_profile_starts_v1_loop_and_holds_profile_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.executor import runtime as runtime_module

    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    runtime = build_executor_runtime(
        Settings(workspace_root=workspace, state_dir=state_dir)
    )
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    runtime.profile_store.save(profile)
    _FakeControlClient.hello_seen = asyncio.Event()
    monkeypatch.setattr(
        runtime_module, "ExecutorControlClient", _FakeControlClient
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
async def test_executor_runtime_restart_reuses_same_profile_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.executor import runtime as runtime_module

    settings = Settings(
        workspace_root=tmp_path / "workspace", state_dir=tmp_path / "state"
    )
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    seed = build_executor_runtime(settings)
    seed.profile_store.save(profile)
    monkeypatch.setattr(
        runtime_module, "ExecutorControlClient", _FakeControlClient
    )

    for _ in range(2):
        _FakeControlClient.hello_seen = asyncio.Event()
        runtime = build_executor_runtime(settings)
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
