import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

import workgate.control.http.tool_routes as http_tool_routes_module
import workgate.ops.shell as shell_ops
from tests.helpers import (
    mcp_structured,
)
from tests.helpers import (
    python_shell_command as _python_shell_command,
)
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp
from workgate.ops.shell import (
    SHELL_TIMEOUT_CLEANUP_GRACE_S,
    _shared_tail_bytes,
    _shell_command_args,
    _subprocess_env,
    _tmux_session_name,
    check_command_policy,
    clamp_timeout,
    read_persistent_shell_output_execute,
    resize_persistent_shell_execute,
    run_shell,
    run_shell_command_timeout,
    send_persistent_shell_input_execute,
    tool_timeout_s,
)
from workgate.schemas.result_models.shell import CommandResult
from workgate.tool_session.store import get_tool_session_store
from workgate.tools.registry import files as fs_tools_module


def test_shell_command_args_are_native_for_supported_shells():
    assert _shell_command_args("/bin/bash", "echo hi") == [
        "/bin/bash",
        "-lc",
        "echo hi",
    ]
    assert _shell_command_args("cmd.exe", "echo hi") == [
        "cmd.exe",
        "/D",
        "/S",
        "/C",
        "echo hi",
    ]
    assert _shell_command_args("pwsh.exe", "Write-Output hi") == [
        "pwsh.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Write-Output hi",
    ]


def test_bounded_runner_uses_trusted_absolute_script(monkeypatch):
    monkeypatch.setattr(shell_ops, "_is_frozen_app", lambda: False)

    argv = shell_ops._bounded_runner_argv("/bin/sh", "echo hi")

    assert argv[0] == sys.executable
    assert os.path.isabs(argv[1])
    assert argv[1].endswith(
        os.path.join("workgate", "ops", "utils", "bounded_runner.py")
    )
    assert "-m" not in argv


def test_bounded_runner_uses_frozen_cli_subcommand(monkeypatch):
    monkeypatch.setattr(shell_ops, "_is_frozen_app", lambda: True)
    monkeypatch.setattr(shell_ops.sys, "executable", "/app/workgate")

    assert shell_ops._bounded_runner_argv("/bin/sh", "echo hi") == [
        "/app/workgate",
        "bounded-runner",
        "--shell",
        "/bin/sh",
        "--command",
        "echo hi",
    ]


def test_persistent_shell_ids_accepts_models_and_compatibility_dicts():
    assert shell_ops._persistent_shell_ids(
        SimpleNamespace(
            shells=[
                SimpleNamespace(shell_id="model-shell"),
                {"shell_id": "dict-shell"},
                {"session_id": "legacy-shell"},
            ]
        )
    ) == {"model-shell", "dict-shell", "legacy-shell"}


@pytest.mark.asyncio
async def test_persistent_shell_creation_is_serialized(monkeypatch):
    monkeypatch.setattr(shell_ops, "_PERSISTENT_SHELL_CREATION_LOCK", None)
    active = 0
    peak = 0

    async def fake_start(
        cwd=".",
        name=None,
        command=None,
        *,
        owner_session_id=None,
        shell_id=None,
        preserve_shell_ids=None,
    ):
        nonlocal active, peak
        assert owner_session_id is None
        assert shell_id is None
        assert preserve_shell_ids is None
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return (cwd, name, command)

    monkeypatch.setattr(shell_ops, "_start_persistent_shell_locked", fake_start)

    results = await asyncio.gather(
        shell_ops.start_persistent_shell_execute(".", "one", "echo one"),
        shell_ops.start_persistent_shell_execute(".", "two", "echo two"),
    )

    assert peak == 1
    assert results == [
        (".", "one", "echo one"),
        (".", "two", "echo two"),
    ]


@pytest.mark.asyncio
async def test_persistent_shell_admission_reenters_only_in_same_task(
    monkeypatch,
):
    gate = asyncio.Lock()
    events: list[tuple[str, str]] = []
    child_entered = asyncio.Event()

    @contextlib.asynccontextmanager
    async def fake_cross_process_lock(namespace: str, key: str):
        async with gate:
            events.append(("enter", f"{namespace}:{key}"))
            try:
                yield
            finally:
                events.append(("exit", f"{namespace}:{key}"))

    async def child():
        async with shell_ops._persistent_shell_admission_lock():
            child_entered.set()

    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops, "cross_process_lock", fake_cross_process_lock
    )

    async with (
        shell_ops._persistent_shell_admission_lock(),
        shell_ops._persistent_shell_admission_lock(),
    ):
        task = asyncio.create_task(child())
        await asyncio.sleep(0)
        assert not child_entered.is_set()

    await task
    assert events == [
        ("enter", "persistent-shell-admission:tmux"),
        ("exit", "persistent-shell-admission:tmux"),
        ("enter", "persistent-shell-admission:tmux"),
        ("exit", "persistent-shell-admission:tmux"),
    ]


@pytest.mark.asyncio
async def test_owned_tmux_shell_is_reserved_before_backend_start(monkeypatch):
    events: list[tuple[str, str]] = []

    class FakeStore:
        def reserve_persistent_shell(
            self,
            session_id: str,
            shell_id: str,
            *,
            exclusive: bool = False,
        ):
            assert exclusive is True
            events.append(("reserve", f"{session_id}:{shell_id}"))
            return True

        def release_session_persistent_shell(
            self, session_id: str, shell_id: str
        ) -> None:
            events.append(("release", f"{session_id}:{shell_id}"))

    async def fake_start(
        cwd=".",
        name=None,
        command=None,
        *,
        owner_session_id=None,
        shell_id=None,
        preserve_shell_ids=None,
    ):
        assert (cwd, name, command) == (".", "owned", "echo ok")
        assert owner_session_id == "SESSION1"
        assert shell_id == "reserved-shell"
        assert preserve_shell_ids is None
        events.append(("start", shell_id))
        return SimpleNamespace(shell_id=shell_id)

    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops, "_tmux_session_name", lambda _name: "reserved-shell"
    )
    monkeypatch.setattr(shell_ops, "get_tool_session_store", FakeStore)
    monkeypatch.setattr(shell_ops, "_start_persistent_shell_locked", fake_start)

    result = await shell_ops.start_persistent_shell_execute(
        ".",
        "owned",
        "echo ok",
        owner_session_id="SESSION1",
    )

    assert result.shell_id == "reserved-shell"
    assert events == [
        ("reserve", "SESSION1:reserved-shell"),
        ("start", "reserved-shell"),
    ]


@pytest.mark.asyncio
async def test_owned_conpty_shell_uses_shared_admission_and_reservation(
    monkeypatch,
):
    events: list[tuple[str, str]] = []

    class FakeStore:
        def reserve_persistent_shell(
            self,
            session_id: str,
            shell_id: str,
            *,
            exclusive: bool = False,
        ) -> bool:
            assert exclusive is True
            events.append(("reserve", f"{session_id}:{shell_id}"))
            return True

    @contextlib.asynccontextmanager
    async def fake_cross_process_lock(namespace: str, key: str):
        events.append(("lock-enter", f"{namespace}:{key}"))
        try:
            yield
        finally:
            events.append(("lock-exit", f"{namespace}:{key}"))

    async def fake_start(
        cwd=".",
        name=None,
        command=None,
        *,
        owner_session_id=None,
        shell_id=None,
    ):
        assert (cwd, name, command) == (".", "owned", "echo ok")
        assert owner_session_id == "SESSION1"
        assert shell_id == "conpty-shell"
        assert shell_ops._PERSISTENT_SHELL_PRESERVE_IDS.get() == frozenset(
            {"conpty-shell"}
        )
        events.append(("start", shell_id))
        return SimpleNamespace(shell_id=shell_id)

    monkeypatch.setattr(shell_ops, "_PERSISTENT_SHELL_CREATION_LOCK", None)
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: True
    )
    monkeypatch.setattr(
        shell_ops, "_tmux_session_name", lambda _name: "conpty-shell"
    )
    monkeypatch.setattr(
        shell_ops, "cross_process_lock", fake_cross_process_lock
    )
    monkeypatch.setattr(shell_ops, "get_tool_session_store", FakeStore)
    monkeypatch.setattr(shell_ops, "_start_persistent_shell_locked", fake_start)

    result = await shell_ops.start_persistent_shell_execute(
        ".", "owned", "echo ok", owner_session_id="SESSION1"
    )

    assert result.shell_id == "conpty-shell"
    assert events == [
        (
            "lock-enter",
            "persistent-shell-admission:conpty",
        ),
        ("reserve", "SESSION1:conpty-shell"),
        ("start", "conpty-shell"),
        (
            "lock-exit",
            "persistent-shell-admission:conpty",
        ),
    ]


@pytest.mark.asyncio
async def test_tmux_reconciliation_preserves_inflight_reservation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    session = store.create_session(workdir=tmp_path)
    assert store.reserve_persistent_shell(
        session.session_id, "reserved-shell", exclusive=True
    )

    async def fake_tmux(args: list[str], timeout_s: int = 10):
        _ = timeout_s
        assert args[0] == "list-sessions"
        return CommandResult(
            ok=True,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            cwd=str(tmp_path),
            command="tmux",
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "resolve_tmux",
        lambda: SimpleNamespace(path="/usr/bin/tmux", source="system"),
    )
    monkeypatch.setattr(shell_ops, "tmux", fake_tmux)

    token = shell_ops._PERSISTENT_SHELL_PRESERVE_IDS.set(
        frozenset({"reserved-shell"})
    )
    try:
        assert (
            await shell_ops.authoritative_persistent_shell_ids_execute()
            == set()
        )
    finally:
        shell_ops._PERSISTENT_SHELL_PRESERVE_IDS.reset(token)

    assert store.require_session(session.session_id).persistent_shell_ids == (
        "reserved-shell",
    )
    assert await shell_ops.authoritative_persistent_shell_ids_execute() == set()
    assert store.require_session(session.session_id).persistent_shell_ids == ()


@pytest.mark.asyncio
async def test_owned_tmux_shell_rolls_back_reservation_after_confirmed_failure(
    monkeypatch,
):
    events: list[tuple[str, str]] = []

    class FakeStore:
        def reserve_persistent_shell(
            self,
            session_id: str,
            shell_id: str,
            *,
            exclusive: bool = False,
        ):
            assert exclusive is True
            events.append(("reserve", f"{session_id}:{shell_id}"))
            return True

        def release_session_persistent_shell(
            self, session_id: str, shell_id: str
        ) -> None:
            events.append(("release", f"{session_id}:{shell_id}"))

    async def failing_start(*_args, **_kwargs):
        events.append(("start", "reserved-shell"))
        raise RuntimeError("backend start failed")

    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops, "_tmux_session_name", lambda _name: "reserved-shell"
    )
    monkeypatch.setattr(shell_ops, "get_tool_session_store", FakeStore)
    monkeypatch.setattr(
        shell_ops, "_start_persistent_shell_locked", failing_start
    )

    with pytest.raises(RuntimeError, match="backend start failed"):
        await shell_ops.start_persistent_shell_execute(
            ".", "owned", owner_session_id="SESSION1"
        )

    assert events == [
        ("reserve", "SESSION1:reserved-shell"),
        ("start", "reserved-shell"),
        ("release", "SESSION1:reserved-shell"),
    ]


@pytest.mark.asyncio
async def test_owned_tmux_shell_keeps_reservation_when_cleanup_is_uncertain(
    monkeypatch,
):
    events: list[tuple[str, str]] = []

    class FakeStore:
        def reserve_persistent_shell(
            self,
            session_id: str,
            shell_id: str,
            *,
            exclusive: bool = False,
        ):
            assert exclusive is True
            events.append(("reserve", f"{session_id}:{shell_id}"))
            return True

        def release_session_persistent_shell(
            self, session_id: str, shell_id: str
        ) -> None:
            events.append(("release", f"{session_id}:{shell_id}"))

    async def uncertain_start(*_args, **_kwargs):
        events.append(("start", "reserved-shell"))
        raise shell_ops.PersistentShellCleanupUncertainError(
            "cleanup not confirmed"
        )

    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops, "_tmux_session_name", lambda _name: "reserved-shell"
    )
    monkeypatch.setattr(shell_ops, "get_tool_session_store", FakeStore)
    monkeypatch.setattr(
        shell_ops, "_start_persistent_shell_locked", uncertain_start
    )

    with pytest.raises(
        shell_ops.PersistentShellCleanupUncertainError,
        match="cleanup not confirmed",
    ):
        await shell_ops.start_persistent_shell_execute(
            ".", "owned", owner_session_id="SESSION1"
        )

    assert events == [
        ("reserve", "SESSION1:reserved-shell"),
        ("start", "reserved-shell"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation_added", [True, False])
async def test_owned_tmux_name_collision_preserves_existing_ownership(
    tmp_path, monkeypatch, reservation_added
):
    events: list[tuple[str, str]] = []

    class FakeStore:
        def reserve_persistent_shell(
            self,
            session_id: str,
            shell_id: str,
            *,
            exclusive: bool = False,
        ):
            assert exclusive is True
            events.append(("reserve", f"{session_id}:{shell_id}"))
            return reservation_added

        def release_session_persistent_shell(
            self, session_id: str, shell_id: str
        ) -> None:
            events.append(("release", f"{session_id}:{shell_id}"))

    async def active_inventory(*, preserve_shell_ids=None):
        assert preserve_shell_ids == {"existing-shell"}
        return {"existing-shell"}

    async def unexpected_tmux(*_args, **_kwargs):
        raise AssertionError("a name collision must not call tmux")

    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops, "_tmux_session_name", lambda _name: "existing-shell"
    )
    monkeypatch.setattr(
        shell_ops,
        "_authoritative_persistent_shell_ids_locked",
        active_inventory,
    )
    monkeypatch.setattr(
        shell_ops,
        "get_settings",
        lambda: SimpleNamespace(max_tmux_sessions=8),
    )
    monkeypatch.setattr(
        shell_ops, "resolve_path", lambda *_args, **_kwargs: tmp_path
    )
    monkeypatch.setattr(shell_ops, "get_tool_session_store", FakeStore)
    monkeypatch.setattr(shell_ops, "tmux", unexpected_tmux)

    with pytest.raises(RuntimeError, match="already exists: existing-shell"):
        await shell_ops.start_persistent_shell_execute(
            str(tmp_path), "existing", owner_session_id="SESSION1"
        )

    expected = [("reserve", "SESSION1:existing-shell")]
    if reservation_added:
        expected.append(("release", "SESSION1:existing-shell"))
    assert events == expected


@pytest.mark.skipif(os.name == "nt", reason="requires the shared tmux backend")
def test_persistent_shell_capacity_is_shared_across_processes(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / ".state"
    active_path = tmp_path / "active-shells"
    barrier_path = tmp_path / "start-both"
    result_paths = [tmp_path / "result-one", tmp_path / "result-two"]
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("WORKGATE_MAX_TMUX_SESSIONS", "1")
    clear_settings_cache()

    script = r"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import workgate.ops.shell as shell_ops
from workgate.config.settings import clear_settings_cache

workspace = Path(__import__("sys").argv[1])
active_path = Path(__import__("sys").argv[2])
barrier_path = Path(__import__("sys").argv[3])
result_path = Path(__import__("sys").argv[4])
shell_name = __import__("sys").argv[5]
clear_settings_cache()
shell_ops._PERSISTENT_SHELL_CREATION_LOCK = None
shell_ops._use_conpty_persistent_shell_backend = lambda: False
shell_ops.resolve_path = lambda *_args, **_kwargs: workspace
shell_ops._tmux_session_name = lambda _name: shell_name
shell_ops._resolved_tmux_shell = lambda _cwd: "/bin/sh"
shell_ops.shutil.which = lambda *_args, **_kwargs: "/bin/sh"
shell_ops.check_command_policy = lambda _command: None
shell_ops.relative_display = lambda _path: "."

async def inventory(*, preserve_shell_ids=None):
    assert preserve_shell_ids == set()
    if not active_path.exists():
        return set()
    return {
        value
        for value in active_path.read_text(encoding="utf-8").splitlines()
        if value
    }

async def fake_tmux(args, timeout_s=10):
    _ = timeout_s
    if args[0] != "new-session":
        raise AssertionError(f"unexpected tmux call: {args}")
    current = []
    if active_path.exists():
        current = [
            value
            for value in active_path.read_text(encoding="utf-8").splitlines()
            if value
        ]
    current.append(args[args.index("-s") + 1])
    active_path.write_text("\n".join(current) + "\n", encoding="utf-8")
    await asyncio.sleep(0.2)
    return SimpleNamespace(ok=True, stdout="", stderr="")

shell_ops._authoritative_persistent_shell_ids_locked = inventory
shell_ops.tmux = fake_tmux
while not barrier_path.exists():
    time.sleep(0.01)
try:
    output = asyncio.run(
        shell_ops.start_persistent_shell_execute(
            ".", shell_name, "sleep 1"
        )
    )
except Exception as exc:
    result_path.write_text(f"error:{exc}", encoding="utf-8")
else:
    result_path.write_text(f"ok:{output.shell_id}", encoding="utf-8")
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(workspace),
                str(active_path),
                str(barrier_path),
                str(result_path),
                name,
            ],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for result_path, name in zip(result_paths, ("one", "two"), strict=True)
    ]
    try:
        barrier_path.write_text("start", encoding="utf-8")
        for process in processes:
            assert process.wait(timeout=10) == 0
        results = [path.read_text(encoding="utf-8") for path in result_paths]
        assert sum(result.startswith("ok:") for result in results) == 1
        errors = [result for result in results if result.startswith("error:")]
        assert len(errors) == 1
        assert "Refusing to start more than 1 persistent shell" in errors[0]
        assert len(active_path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize(
    ("owner_bound", "names", "max_sessions", "error_text"),
    [
        (
            False,
            ("conpty-one", "conpty-two"),
            1,
            "Refusing to start more than 1 persistent shell",
        ),
        (
            True,
            ("shared-conpty", "shared-conpty"),
            2,
            "already reserved by another session",
        ),
    ],
)
def test_conpty_admission_is_shared_across_processes(
    tmp_path,
    monkeypatch,
    owner_bound,
    names,
    max_sessions,
    error_text,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / ".state"
    barrier_path = tmp_path / "start-both"
    result_paths = [tmp_path / "result-one", tmp_path / "result-two"]
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("WORKGATE_MAX_TMUX_SESSIONS", str(max_sessions))
    clear_settings_cache()

    script = r"""
import asyncio
import time
from pathlib import Path

import workgate.ops.shell as shell_ops
import workgate.terminal.conpty as conpty
from workgate.config.settings import clear_settings_cache
from workgate.terminal.runtime import build_terminal_runtime
from workgate.tool_session.store import get_tool_session_store

workspace = Path(__import__("sys").argv[1])
barrier_path = Path(__import__("sys").argv[2])
result_path = Path(__import__("sys").argv[3])
shell_name = __import__("sys").argv[4]
owner_bound = __import__("sys").argv[5] == "1"
clear_settings_cache()
shell_ops._PERSISTENT_SHELL_CREATION_LOCK = None
shell_ops._use_conpty_persistent_shell_backend = lambda: True
shell_ops.resolve_path = lambda *_args, **_kwargs: workspace
shell_ops._tmux_session_name = lambda name: str(name)
shell_ops.check_command_policy = lambda _command: None
conpty.is_available = lambda: True
conpty.relative_display = lambda path: str(path)

class FakePty:
    exitstatus = None

    def __init__(self):
        self.alive = True

    def isalive(self):
        return self.alive

    def read(self, _size=None):
        time.sleep(0.02)
        return ""

    def close(self, force=False):
        _ = force
        self.alive = False

conpty._spawn_pty = lambda *_args: FakePty()
owner_session_id = None
if owner_bound:
    owner_session_id = get_tool_session_store().create_session(
        workdir=workspace
    ).session_id
while not barrier_path.exists():
    time.sleep(0.01)

async def main():
    runtime = build_terminal_runtime()
    await runtime.start()
    try:
        output = await shell_ops.start_persistent_shell_execute(
            ".", shell_name, "echo ready", owner_session_id=owner_session_id
        )
    except Exception as exc:
        result_path.write_text(f"error:{exc}", encoding="utf-8")
    else:
        result_path.write_text(f"ok:{output.shell_id}", encoding="utf-8")
        await asyncio.sleep(0.75)
    finally:
        await runtime.aclose()

asyncio.run(main())
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(workspace),
                str(barrier_path),
                str(result_path),
                name,
                "1" if owner_bound else "0",
            ],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for result_path, name in zip(result_paths, names, strict=True)
    ]
    try:
        barrier_path.write_text("start", encoding="utf-8")
        for process in processes:
            assert process.wait(timeout=10) == 0
        results = [path.read_text(encoding="utf-8") for path in result_paths]
        assert sum(result.startswith("ok:") for result in results) == 1
        errors = [result for result in results if result.startswith("error:")]
        assert len(errors) == 1
        assert error_text in errors[0]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.asyncio
async def test_tmux_start_cancellation_cleans_created_session(
    tmp_path, monkeypatch
):
    owner_tag_started = asyncio.Event()
    block_owner_tag = asyncio.Event()
    calls: list[list[str]] = []

    def result(*, ok: bool = True, stderr: str = "") -> CommandResult:
        return CommandResult(
            ok=ok,
            exit_code=0 if ok else 1,
            timed_out=False,
            duration_ms=1,
            cwd=str(tmp_path),
            command="tmux",
            stdout="",
            stderr=stderr,
        )

    async def empty_inventory(*, preserve_shell_ids=None):
        assert preserve_shell_ids is None
        return set()

    async def fake_tmux(args: list[str], timeout_s: int = 10):  # noqa: ARG001
        calls.append(args)
        if args[0] == "new-session":
            assert args[-6:] == [
                ";",
                "set-option",
                "-t",
                "owned",
                shell_ops._TMUX_OWNER_OPTION,
                "SESSION1",
            ]
            owner_tag_started.set()
            await block_owner_tag.wait()
            return result()
        if args[0] == "kill-session":
            return result()
        raise AssertionError(f"unexpected tmux call: {args}")

    monkeypatch.setattr(
        shell_ops, "authoritative_persistent_shell_ids_execute", empty_inventory
    )
    monkeypatch.setattr(
        shell_ops,
        "get_settings",
        lambda: SimpleNamespace(max_tmux_sessions=8),
    )
    monkeypatch.setattr(
        shell_ops, "resolve_path", lambda *_args, **_kwargs: tmp_path
    )
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(shell_ops, "_tmux_session_name", lambda _name: "owned")
    monkeypatch.setattr(
        shell_ops, "_resolved_tmux_shell", lambda _cwd: "/bin/sh"
    )
    monkeypatch.setattr(
        shell_ops.shutil, "which", lambda *_args, **_kwargs: "/bin/sh"
    )
    monkeypatch.setattr(
        shell_ops, "check_command_policy", lambda _command: None
    )
    monkeypatch.setattr(shell_ops, "tmux", fake_tmux)

    task = asyncio.create_task(
        shell_ops._start_persistent_shell_locked(
            str(tmp_path),
            "owned",
            "sleep 60",
            owner_session_id="SESSION1",
        )
    )
    await owner_tag_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert ["kill-session", "-t", "=owned"] in calls


@pytest.mark.asyncio
async def test_failed_tmux_start_cleanup_must_be_authoritative(
    tmp_path, monkeypatch
):
    async def failed_cleanup(_args: list[str], timeout_s: int = 10):  # noqa: ARG001
        return CommandResult(
            ok=False,
            exit_code=1,
            timed_out=False,
            duration_ms=1,
            cwd=str(tmp_path),
            command="tmux",
            stdout="",
            stderr="permission denied",
        )

    monkeypatch.setattr(shell_ops, "tmux", failed_cleanup)

    with pytest.raises(RuntimeError, match="permission denied"):
        await shell_ops._cleanup_failed_tmux_start("owned")


@pytest.mark.parametrize(
    "absent_error",
    [
        "no server running on /tmp/tmux/default",
        "failed to connect to server: No such file or directory",
        (
            "error connecting to /tmp/isolated/tmux-1000/default "
            "(No such file or directory)"
        ),
    ],
)
@pytest.mark.asyncio
async def test_list_persistent_shells_clears_stale_owners_when_server_is_absent(
    absent_error,
    monkeypatch,
):
    reconciled: list[set[str]] = []
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )

    async def fake_tmux(_args, timeout_s=10):  # noqa: ARG001
        return CommandResult(
            ok=False,
            exit_code=1,
            duration_ms=1,
            cwd=".",
            command="tmux list-sessions",
            stderr=absent_error,
        )

    monkeypatch.setattr(
        shell_ops,
        "resolve_tmux",
        lambda: SimpleNamespace(path="/usr/bin/tmux", source="system"),
    )
    monkeypatch.setattr(shell_ops, "tmux", fake_tmux)
    monkeypatch.setattr(
        shell_ops,
        "get_tool_session_store",
        lambda: SimpleNamespace(
            reconcile_persistent_shells=lambda shell_ids: reconciled.append(
                shell_ids
            )
        ),
    )

    result = await shell_ops.list_persistent_shells_execute()

    assert result.shells == []
    assert reconciled == [set()]


@pytest.mark.asyncio
async def test_list_persistent_shells_preserves_owners_on_unknown_tmux_failure(
    monkeypatch,
):
    reconciled: list[set[str]] = []
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )

    async def fake_tmux(_args, timeout_s=10):  # noqa: ARG001
        return CommandResult(
            ok=False,
            exit_code=1,
            duration_ms=1,
            cwd=".",
            command="tmux list-sessions",
            stderr="permission denied",
        )

    monkeypatch.setattr(
        shell_ops,
        "resolve_tmux",
        lambda: SimpleNamespace(path="/usr/bin/tmux", source="system"),
    )
    monkeypatch.setattr(shell_ops, "tmux", fake_tmux)
    monkeypatch.setattr(
        shell_ops,
        "get_tool_session_store",
        lambda: SimpleNamespace(
            reconcile_persistent_shells=lambda shell_ids: reconciled.append(
                shell_ids
            )
        ),
    )

    result = await shell_ops.list_persistent_shells_execute()

    assert result.shells == []
    assert reconciled == []


@pytest.mark.asyncio
async def test_list_owned_shells_returns_none_on_unknown_tmux_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "resolve_tmux",
        lambda: SimpleNamespace(path="/usr/bin/tmux", source="system"),
    )

    async def failed_tmux(_args, timeout_s=10):  # noqa: ARG001
        return CommandResult(
            ok=False,
            exit_code=1,
            duration_ms=1,
            cwd=".",
            command="tmux list-sessions",
            stderr="permission denied",
        )

    monkeypatch.setattr(shell_ops, "tmux", failed_tmux)

    assert (
        await shell_ops.list_owned_persistent_shell_ids_execute("SESSION1")
        is None
    )


@pytest.mark.asyncio
async def test_list_owned_shells_treats_absent_server_as_authoritative_empty(
    monkeypatch,
):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "resolve_tmux",
        lambda: SimpleNamespace(path="/usr/bin/tmux", source="system"),
    )

    async def absent_tmux(_args, timeout_s=10):  # noqa: ARG001
        return CommandResult(
            ok=False,
            exit_code=1,
            duration_ms=1,
            cwd=".",
            command="tmux list-sessions",
            stderr="error connecting to /tmp/tmux/default (No such file or directory)",
        )

    monkeypatch.setattr(shell_ops, "tmux", absent_tmux)

    assert (
        await shell_ops.list_owned_persistent_shell_ids_execute("SESSION1")
        == []
    )


@pytest.mark.asyncio
async def test_list_owned_shells_filters_owner_from_authoritative_inventory(
    monkeypatch,
):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "resolve_tmux",
        lambda: SimpleNamespace(path="/usr/bin/tmux", source="system"),
    )

    async def listed_tmux(_args, timeout_s=10):  # noqa: ARG001
        return CommandResult(
            ok=True,
            exit_code=0,
            duration_ms=1,
            cwd=".",
            command="tmux list-sessions",
            stdout="owned\tSESSION1\nother\tSESSION2\n",
        )

    monkeypatch.setattr(shell_ops, "tmux", listed_tmux)

    assert await shell_ops.list_owned_persistent_shell_ids_execute(
        "SESSION1"
    ) == ["owned"]


@pytest.mark.asyncio
async def test_conpty_owned_inventory_is_unknown_for_peer_durable_shell(
    monkeypatch,
):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: True
    )

    async def no_local_shells(_owner_session_id: str):
        return []

    monkeypatch.setattr(
        shell_ops.conpty, "list_owned_shell_ids", no_local_shells
    )
    monkeypatch.setattr(
        shell_ops.conpty, "authoritative_shell_ids", lambda: {"peer-shell"}
    )
    monkeypatch.setattr(
        shell_ops,
        "get_tool_session_store",
        lambda: SimpleNamespace(
            require_session=lambda _session_id: SimpleNamespace(
                persistent_shell_ids=("peer-shell",)
            ),
            release_session_persistent_shell=lambda *_args: None,
        ),
    )

    assert (
        await shell_ops.list_owned_persistent_shell_ids_execute("SESSION1")
        is None
    )


@pytest.mark.asyncio
async def test_conpty_owned_inventory_accepts_current_process_shells(
    monkeypatch,
):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: True
    )

    async def local_shells(_owner_session_id: str):
        return ["local-shell"]

    monkeypatch.setattr(shell_ops.conpty, "list_owned_shell_ids", local_shells)
    monkeypatch.setattr(
        shell_ops.conpty, "authoritative_shell_ids", lambda: {"local-shell"}
    )
    monkeypatch.setattr(
        shell_ops,
        "get_tool_session_store",
        lambda: SimpleNamespace(
            require_session=lambda _session_id: SimpleNamespace(
                persistent_shell_ids=("local-shell",)
            ),
            release_session_persistent_shell=lambda *_args: None,
        ),
    )

    assert await shell_ops.list_owned_persistent_shell_ids_execute(
        "SESSION1"
    ) == ["local-shell"]


@pytest.mark.asyncio
async def test_conpty_owned_inventory_clears_dead_peer_reservation(monkeypatch):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: True
    )

    async def no_local_shells(_owner_session_id: str):
        return []

    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        shell_ops.conpty, "list_owned_shell_ids", no_local_shells
    )
    monkeypatch.setattr(
        shell_ops.conpty, "authoritative_shell_ids", lambda: set()
    )
    monkeypatch.setattr(
        shell_ops,
        "get_tool_session_store",
        lambda: SimpleNamespace(
            require_session=lambda _session_id: SimpleNamespace(
                persistent_shell_ids=("dead-peer-shell",)
            ),
            release_session_persistent_shell=lambda session_id, shell_id: (
                released.append((session_id, shell_id))
            ),
        ),
    )

    assert (
        await shell_ops.list_owned_persistent_shell_ids_execute("SESSION1")
        == []
    )
    assert released == [("SESSION1", "dead-peer-shell")]


@pytest.mark.asyncio
async def test_conpty_authoritative_inventory_reconciles_from_live_leases(
    monkeypatch,
):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: True
    )

    reconciled: list[set[str]] = []
    monkeypatch.setattr(
        shell_ops.conpty,
        "authoritative_shell_ids",
        lambda: {"local-shell", "peer-shell"},
    )
    monkeypatch.setattr(
        shell_ops,
        "get_tool_session_store",
        lambda: SimpleNamespace(
            reconcile_persistent_shells=lambda shell_ids: reconciled.append(
                set(shell_ids)
            ),
        ),
    )

    assert await shell_ops.authoritative_persistent_shell_ids_execute() == {
        "local-shell",
        "peer-shell",
    }
    assert reconciled == [{"local-shell", "peer-shell"}]


@pytest.mark.asyncio
async def test_conpty_authoritative_inventory_clears_dead_durable_ids(
    monkeypatch,
):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: True
    )
    reconciled: list[set[str]] = []
    monkeypatch.setattr(
        shell_ops.conpty, "authoritative_shell_ids", lambda: {"local-shell"}
    )
    monkeypatch.setattr(
        shell_ops,
        "get_tool_session_store",
        lambda: SimpleNamespace(
            reconcile_persistent_shells=lambda shell_ids: reconciled.append(
                set(shell_ids)
            ),
        ),
    )

    assert await shell_ops.authoritative_persistent_shell_ids_execute() == {
        "local-shell"
    }
    assert reconciled == [{"local-shell"}]


@pytest.mark.asyncio
async def test_conpty_listing_does_not_reconcile_peer_durable_ids(monkeypatch):
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: True
    )

    async def no_local_shells():
        return SimpleNamespace(shells=[])

    monkeypatch.setattr(shell_ops.conpty, "list_shells", no_local_shells)
    monkeypatch.setattr(
        shell_ops,
        "get_tool_session_store",
        lambda: SimpleNamespace(
            reconcile_persistent_shells=lambda _ids: pytest.fail(
                "process-local ConPTY inventory must not reconcile durable peers"
            )
        ),
    )

    result = await shell_ops.list_persistent_shells_execute()
    assert result.shells == []


def test_command_with_env_uses_powershell_assignments(monkeypatch):
    monkeypatch.setattr(
        shell_ops, "_effective_shell_executable", lambda: "pwsh.exe"
    )

    command = shell_ops._command_with_env(
        "Write-Output ok", {"TOKEN": "O'Reilly"}
    )

    assert command == "$env:TOKEN='O''Reilly'; Write-Output ok"


def test_command_with_env_uses_cmd_assignments(monkeypatch):
    monkeypatch.setattr(
        shell_ops, "_effective_shell_executable", lambda: "cmd.exe"
    )

    command = shell_ops._command_with_env("echo ok", {"TOKEN": "a b"})

    assert command == 'set "TOKEN=a b" && echo ok'


def test_command_denylist_matching_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("WORKGATE_COMMAND_DENYLIST", "RM -RF")
    clear_settings_cache()

    with pytest.raises(PermissionError, match="denylisted fragment"):
        check_command_policy("rm -rf /tmp/example")


def test_tmux_session_name_strips_invalid_edges_and_has_safe_fallback():
    assert _tmux_session_name("  ..example shell--  ") == "example-shell"

    fallback = _tmux_session_name("..--")
    assert fallback.startswith("mcp-")
    assert len(fallback) <= 64


@pytest.mark.asyncio
async def test_bash_rejects_timeout_above_public_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    mcp = build_mcp()
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )

    with pytest.raises(ToolError, match="timeout_s must be <= 120 seconds"):
        await mcp.call_tool(
            "bash",
            {
                "session_id": session["session_id"],
                "command": "echo ok",
                "timeout_s": 3600,
            },
        )


def test_shell_tool_watchdog_reserves_cleanup_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "0.01")
    monkeypatch.setenv("WORKGATE_RUN_SHELL_MAX_TIMEOUT_S", "1")
    clear_settings_cache()

    assert SHELL_TIMEOUT_CLEANUP_GRACE_S == 10
    assert tool_timeout_s("list_files") == 0.01
    assert tool_timeout_s("bash") == 11
    assert tool_timeout_s("run_python_code") == 11


@pytest.mark.asyncio
async def test_mcp_shell_timeout_returns_partial_output_after_cleanup(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    mcp = build_mcp()
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )
    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "0.01")
    monkeypatch.setenv("WORKGATE_RUN_SHELL_MAX_TIMEOUT_S", "3")
    clear_settings_cache()
    command = _python_shell_command(
        'import sys, time; print("partial-out", flush=True); '
        'print("partial-err", file=sys.stderr, flush=True); time.sleep(10)'
    )

    payload = mcp_structured(
        await mcp.call_tool(
            "bash",
            {
                "session_id": session["session_id"],
                "command": command,
                "timeout_s": 3,
            },
        )
    )
    result = payload["result"]

    assert payload["mode"] == "command"
    assert result["timed_out"] is True
    assert "partial-out" in result["stdout"]
    assert "partial-err" in result["stderr"]


def test_rest_shell_timeout_returns_partial_output_after_cleanup(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "0.01")
    monkeypatch.setenv("WORKGATE_RUN_SHELL_MAX_TIMEOUT_S", "3")
    clear_settings_cache()

    client = TestClient(build_http_app())
    session_id = get_tool_session_store().create_session(workdir=".").session_id
    command = _python_shell_command(
        'import sys, time; print("partial-out", flush=True); '
        'print("partial-err", file=sys.stderr, flush=True); time.sleep(10)'
    )
    response = client.post(
        "/tools/bash",
        json={
            "session_id": session_id,
            "command": command,
            "timeout_s": 3,
        },
    )
    result = response.json()["result"]

    assert response.status_code == 200
    assert result["timed_out"] is True
    assert "partial-out" in result["stdout"]
    assert "partial-err" in result["stderr"]


def test_rest_tool_watchdog_returns_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "0.01")
    clear_settings_cache()

    async def hanging_call_local_tool(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(
        http_tool_routes_module, "call_http_tool", hanging_call_local_tool
    )

    response = TestClient(build_http_app()).post(
        "/tools/list_files", json={"path": "."}
    )

    assert response.status_code == 504
    assert response.json()["error"] == "tool_timeout"


def test_rest_tool_watchdog_times_out_sync_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()

    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "0.01")
    clear_settings_cache()

    async def blocking_list_dir(*args, **kwargs):
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(
        fs_tools_module, "list_files_dispatch_execute", blocking_list_dir
    )
    response = client.post(
        "/tools/list_files",
        json={"session_id": session["session_id"], "path": "."},
    )

    assert response.status_code == 504
    assert response.json()["error"] == "tool_timeout"


def test_rest_tool_watchdog_preserves_file_and_todo_mutations(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "0.01")
    clear_settings_cache()

    async def delayed_call_http_tool(tool_name, args, **_kwargs):
        await asyncio.sleep(0.05)
        return {"tool": tool_name}

    monkeypatch.setattr(
        http_tool_routes_module, "call_http_tool", delayed_call_http_tool
    )
    client = TestClient(build_http_app())

    write_response = client.post("/tools/write_file", json={})
    todo_write_response = client.post("/tools/todo", json={})
    todo_read_response = client.get("/tools/todo")

    assert write_response.status_code == 200
    assert write_response.json() == {"tool": "write_file"}
    assert todo_write_response.status_code == 200
    assert todo_write_response.json() == {"tool": "write_todos"}
    assert todo_read_response.status_code == 504
    assert todo_read_response.json()["error"] == "tool_timeout"


def test_rest_readyz_does_not_expose_workspace_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()

    response = TestClient(build_http_app()).get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_mcp_tool_watchdog_times_out_sync_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    mcp = build_mcp()
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )

    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "0.01")
    clear_settings_cache()

    async def blocking_list_dir(*args, **kwargs):
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(
        fs_tools_module, "list_files_dispatch_execute", blocking_list_dir
    )
    with pytest.raises(
        ToolError, match="list_files exceeded 0.01 second tool timeout"
    ):
        await mcp.call_tool(
            "list_files", {"session_id": session["session_id"], "path": "."}
        )


def test_apply_patch_watchdog_covers_both_git_phases(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "15")
    clear_settings_cache()

    assert tool_timeout_s("apply_patch") == 45
    assert tool_timeout_s("list_files") == 15


def test_run_shell_command_timeout_uses_ten_second_default(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_RUN_SHELL_DEFAULT_TIMEOUT_S", "10")
    clear_settings_cache()

    assert run_shell_command_timeout(None) == 10


@pytest.mark.asyncio
async def test_spawn_process_uses_native_shell_api_for_cmd_on_windows(
    monkeypatch,
):
    calls = []
    sentinel = object()

    async def fake_shell(command: str, **kwargs):
        calls.append((command, kwargs))
        return sentinel

    async def unexpected_exec(*_args, **_kwargs):
        raise AssertionError("cmd.exe must use create_subprocess_shell")

    monkeypatch.setattr(shell_ops.os, "name", "nt")
    monkeypatch.setattr(
        shell_ops, "new_process_group_kwargs", lambda: {"creationflags": 512}
    )
    monkeypatch.setattr(
        shell_ops, "_effective_shell_executable", lambda: "cmd.exe"
    )
    monkeypatch.setattr(shell_ops.shutil, "which", lambda command, **_: command)
    monkeypatch.setattr(shell_ops, "_subprocess_env", lambda: {"BASE": "1"})
    monkeypatch.setattr(
        shell_ops.asyncio, "create_subprocess_shell", fake_shell
    )
    monkeypatch.setattr(
        shell_ops.asyncio, "create_subprocess_exec", unexpected_exec
    )

    result = await shell_ops._spawn_process("echo hi", ".", {"EXTRA": "2"})

    assert result is sentinel
    assert calls[0][0] == "echo hi"
    assert calls[0][1]["executable"] == "cmd.exe"
    assert calls[0][1]["env"] == {"BASE": "1", "EXTRA": "2"}
    assert calls[0][1]["creationflags"] == 512


@pytest.mark.asyncio
async def test_spawn_process_uses_native_exec_for_powershell_on_windows(
    monkeypatch,
):
    calls = []
    sentinel = object()

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(shell_ops.os, "name", "nt")
    monkeypatch.setattr(
        shell_ops, "new_process_group_kwargs", lambda: {"creationflags": 512}
    )
    monkeypatch.setattr(
        shell_ops, "_effective_shell_executable", lambda: "pwsh.exe"
    )
    monkeypatch.setattr(shell_ops.shutil, "which", lambda command, **_: command)
    monkeypatch.setattr(shell_ops.asyncio, "create_subprocess_exec", fake_exec)

    result = await shell_ops._spawn_process("Write-Output hi", ".")

    assert result is sentinel
    assert calls[0][0] == (
        "pwsh.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Write-Output hi",
    )


@pytest.mark.asyncio
async def test_spawn_process_resolves_relative_shell_from_command_cwd(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(shell_ops.os, "name", "posix")
    shell = tmp_path / "bin" / "custom-shell"
    shell.parent.mkdir()
    shell.write_text("#!/bin/sh\n", encoding="utf-8")
    shell.chmod(0o700)
    monkeypatch.setattr(
        shell_ops, "_effective_shell_executable", lambda: "bin/custom-shell"
    )
    monkeypatch.setattr(
        shell_ops.shutil,
        "which",
        lambda command, **_: command,
    )
    calls = []
    sentinel = object()

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(shell_ops.asyncio, "create_subprocess_exec", fake_exec)

    result = await shell_ops._spawn_process("echo hi", str(tmp_path))

    assert result is sentinel
    argv = calls[0][0]
    shell_index = argv.index("--shell") + 1
    assert argv[shell_index] == str(shell)
    assert calls[0][1]["cwd"] == str(tmp_path)


def test_run_shell_command_timeout_allows_explicit_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    assert run_shell_command_timeout(120) == 120


def test_internal_shell_timeout_uses_at_least_builtin_default(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_RUN_SHELL_DEFAULT_TIMEOUT_S", "5")
    clear_settings_cache()

    assert clamp_timeout(None) == 60


def test_internal_shell_timeout_uses_larger_run_shell_values(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_RUN_SHELL_DEFAULT_TIMEOUT_S", "120")
    monkeypatch.setenv("WORKGATE_RUN_SHELL_MAX_TIMEOUT_S", "7200")
    clear_settings_cache()

    assert clamp_timeout(None) == 120
    assert clamp_timeout(9999) == 7200


@pytest.mark.asyncio
async def test_run_shell_command_timeout_includes_subprocess_spawn(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    async def hanging_spawn(
        command: str, cwd: str, env: dict[str, str] | None = None
    ):
        await asyncio.sleep(5)

    monkeypatch.setattr("workgate.ops.shell._spawn_process", hanging_spawn)

    result = await run_shell("echo never", timeout_s=1)

    assert result.ok is False
    assert result.timed_out is True
    assert result.exit_code is None
    assert "Timed out while starting subprocess" in result.stderr


@pytest.mark.asyncio
async def test_run_shell_command_fast_command_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    result = await run_shell("echo ok", timeout_s=5)

    assert result.ok is True
    assert result.timed_out is False
    assert "ok" in result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux subreaper")
@pytest.mark.asyncio
async def test_bounded_command_reaps_same_group_background_child(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    result = await run_shell(
        "sleep 60 >/dev/null 2>&1 & echo $!",
        timeout_s=5,
    )
    pid = int(result.stdout.strip())
    try:
        assert result.ok is True
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux subreaper")
@pytest.mark.asyncio
async def test_bounded_command_reaps_descendant_that_escapes_process_group(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    result = await run_shell(
        "setsid sleep 60 >/dev/null 2>&1 & echo $!",
        timeout_s=5,
    )
    pid = int(result.stdout.strip())
    try:
        assert result.ok is True
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux subreaper")
@pytest.mark.asyncio
async def test_timed_out_bounded_command_reaps_escaped_descendant(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    result = await run_shell(
        "setsid sleep 60 >/dev/null 2>&1 & echo $!; sleep 60",
        timeout_s=1,
    )
    pid = int(result.stdout.strip())
    try:
        assert result.timed_out is True
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_run_shell_command_streams_and_bounds_large_output(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    result = await run_shell(
        _python_shell_command('import sys; sys.stdout.write("x" * 200000)'),
        timeout_s=5,
        max_output_bytes=1000,
    )

    assert result.ok is True
    assert result.truncated is True
    assert len(result.stdout.encode()) == 1000
    assert result.stderr == ""


def test_shared_tail_bytes_uses_idle_stream_capacity_and_preserves_tails():
    stdout, stderr, truncated = _shared_tail_bytes(
        b"prefix-" + b"o" * 900,
        b"e" * 300,
        1000,
    )

    assert truncated is True
    assert len(stdout) == 700
    assert len(stderr) == 300
    assert stdout == b"o" * 700
    assert stderr == b"e" * 300


def test_shared_tail_bytes_gives_remaining_capacity_to_stderr():
    stdout, stderr, truncated = _shared_tail_bytes(
        b"o" * 300,
        b"prefix-" + b"e" * 900,
        1000,
    )

    assert truncated is True
    assert stdout == b"o" * 300
    assert stderr == b"e" * 700


def test_tail_buffer_ignores_empty_chunks():
    tail = shell_ops.TailBuffer(keep_bytes=4, data=bytearray(b"ok"))

    tail.append(b"")

    assert tail.data == bytearray(b"ok")
    assert tail.total_bytes == 0


@pytest.mark.asyncio
async def test_run_shell_uses_unused_stderr_budget_for_stdout(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    result = await run_shell(
        _python_shell_command('import sys; sys.stdout.write("x" * 1500)'),
        timeout_s=5,
        max_output_bytes=2000,
    )

    assert result.ok is True
    assert result.truncated is False
    assert len(result.stdout.encode()) == 1500
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_run_shell_shares_total_budget_between_streams(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    result = await run_shell(
        _python_shell_command(
            'import sys; sys.stdout.write("o" * 900); '
            'sys.stderr.write("e" * 900)'
        ),
        timeout_s=5,
        max_output_bytes=1000,
    )

    assert result.ok is True
    assert result.truncated is True
    assert len(result.stdout.encode()) == 500
    assert len(result.stderr.encode()) == 500
    assert len(result.stdout.encode()) + len(result.stderr.encode()) == 1000


@pytest.mark.asyncio
async def test_run_shell_command_timeout_marks_result_and_cleans_up(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    result = await run_shell("sleep 30", timeout_s=1)

    assert result.ok is False
    assert result.timed_out is True


@pytest.mark.skipif(os.name == "nt", reason="tmux-specific behavior")
@pytest.mark.asyncio
async def test_read_persistent_shell_preserves_ansi_only_when_requested(
    monkeypatch,
):
    calls = []

    async def fake_tmux(args: list[str], timeout_s: int = 10):
        calls.append((args, timeout_s))
        return CommandResult(
            ok=True,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            cwd=".",
            command="tmux",
            stdout="\x1b[32mready\x1b[0m",
        )

    monkeypatch.setattr("workgate.ops.shell.tmux", fake_tmux)

    plain = await read_persistent_shell_output_execute("shell-1", 40)
    colored = await read_persistent_shell_output_execute(
        "shell-1", 40, preserve_ansi=True
    )

    assert plain.output == "\x1b[32mready\x1b[0m"
    assert colored.output == plain.output
    assert calls == [
        (["capture-pane", "-p", "-t", "shell-1", "-S", "-40"], 10),
        (["capture-pane", "-p", "-e", "-t", "shell-1", "-S", "-40"], 10),
    ]


@pytest.mark.skipif(os.name == "nt", reason="tmux-specific behavior")
@pytest.mark.asyncio
async def test_resize_persistent_shell_resizes_tmux_window(monkeypatch):
    calls = []

    async def fake_tmux(args: list[str], timeout_s: int = 10):
        calls.append((args, timeout_s))
        return CommandResult(
            ok=True,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            cwd=".",
            command="tmux",
        )

    monkeypatch.setattr("workgate.ops.shell.tmux", fake_tmux)

    result = await resize_persistent_shell_execute("shell-1", 180, 42)

    assert result.model_dump() == {
        "shell_id": "shell-1",
        "cols": 180,
        "rows": 42,
        "resized": True,
        "backend": "tmux",
    }
    assert calls == [
        (["resize-window", "-t", "shell-1", "-x", "180", "-y", "42"], 10)
    ]


@pytest.mark.asyncio
async def test_resize_persistent_shell_rejects_invalid_dimensions():
    with pytest.raises(ValueError, match="cols must be between"):
        await resize_persistent_shell_execute("shell-1", 10, 24)
    with pytest.raises(ValueError, match="rows must be between"):
        await resize_persistent_shell_execute("shell-1", 80, 2)


@pytest.mark.skipif(os.name == "nt", reason="tmux-specific behavior")
@pytest.mark.asyncio
async def test_send_shell_invokes_tmux_promptly(monkeypatch):
    calls = []

    async def fake_tmux(args: list[str], timeout_s: int = 10):
        calls.append((args, timeout_s))
        return CommandResult(
            ok=True,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            cwd=".",
            command="tmux",
        )

    monkeypatch.setattr("workgate.ops.shell.tmux", fake_tmux)

    result = await asyncio.wait_for(
        send_persistent_shell_input_execute("shell-1", "echo ok", enter=True),
        timeout=1,
    )

    assert result.model_dump() == {
        "shell_id": "shell-1",
        "sent_bytes": 7,
        "enter": True,
    }
    assert calls == [
        (["send-keys", "-l", "-t", "shell-1", "echo ok"], 10),
        (["send-keys", "-t", "shell-1", "Enter"], 10),
    ]


@pytest.mark.asyncio
async def test_run_shell_command_filters_server_environment(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "should-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/app/src")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "should-not-leak")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "should-not-leak")
    clear_settings_cache()

    result = await run_shell(
        _python_shell_command(
            "import os; "
            "blocked = ('PYTHONPATH', 'CLOUDFLARE_TUNNEL_TOKEN'); "
            "keys = [key for key in sorted(os.environ) "
            "if key in blocked or key.startswith('WORKGATE_') "
            "or key.startswith('DOCKER_')]; "
            "print('\\n'.join(f'{key}={os.environ[key]}' for key in keys), end='')"
        ),
        cwd=str(tmp_path),
    )

    assert result.ok
    assert result.stdout == ""


def test_frozen_subprocess_env_restores_loader_environment(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/bundled")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/bundled/libpreload.so")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/tmp/bundled")
    monkeypatch.setenv("DYLD_LIBRARY_PATH_ORIG", "/opt/homebrew/lib")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/bundled/libinject.dylib")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/bundled", raising=False)
    clear_settings_cache()

    env = _subprocess_env()

    assert env["LD_LIBRARY_PATH"] == "/usr/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env
    assert "LD_PRELOAD" not in env
    assert env["DYLD_LIBRARY_PATH"] == "/opt/homebrew/lib"
    assert "DYLD_LIBRARY_PATH_ORIG" not in env
    assert "DYLD_INSERT_LIBRARIES" not in env


def test_non_frozen_subprocess_env_preserves_loader_environment(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/custom/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/original/lib")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    clear_settings_cache()

    env = _subprocess_env()

    assert env["LD_LIBRARY_PATH"] == "/custom/lib"
    assert env["LD_LIBRARY_PATH_ORIG"] == "/original/lib"
