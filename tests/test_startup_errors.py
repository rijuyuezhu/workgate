import asyncio
import io
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

import workgate.ops.shell as shell_ops
import workgate.terminal.conpty as conpty
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp
from workgate.errors import (
    PathNotFoundError,
    ShellExecutableNotFoundError,
    exception_from_tool_error,
    process_start_not_found_error,
    tool_error_payload,
    workspace_path_not_found_error,
)
from workgate.ops.session import session_start_execute
from workgate.ops.utils.path import resolve_path
from workgate.ops.utils.remote_session import _remote_result_data
from workgate.remote.bundle import worker_bundle_bytes
from workgate.remote.manager import RemoteManager, RemoteWorker, _utc
from workgate.remote_worker.worker import _handled_remote_exception
from workgate.schemas.result_models.shell import (
    CommandResult,
)
from workgate.terminal.runtime import build_terminal_runtime
from workgate.terminal.tmux import TmuxSelection
from workgate.tool_session.store import get_tool_session_store


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "false")
    clear_settings_cache()
    get_tool_session_store().clear()


def test_process_start_classifies_missing_cwd_before_executable(
    tmp_path: Path,
) -> None:
    missing_cwd = tmp_path / "vanished"
    exc = FileNotFoundError(2, "No such file or directory", "/bin/sh")

    result = process_start_not_found_error(
        exc,
        executable="/bin/sh",
        command="echo ok",
        cwd=missing_cwd,
    )

    assert isinstance(result, PathNotFoundError)
    assert result.path == missing_cwd


def test_workspace_path_detection_uses_missing_second_endpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    missing_destination = tmp_path / "removed-parent" / "destination.txt"
    exc = FileNotFoundError(2, "No such file or directory", str(source))
    exc.filename2 = str(missing_destination)

    result = workspace_path_not_found_error(exc, tmp_path)

    assert isinstance(result, PathNotFoundError)
    assert result.path == missing_destination


def test_workspace_path_detection_ignores_untrusted_endpoints(
    tmp_path: Path,
) -> None:
    relative = FileNotFoundError(2, "missing", "relative.txt")
    outside = FileNotFoundError(
        2, "missing", str(tmp_path.parent / "outside-workspace")
    )

    assert workspace_path_not_found_error(relative, tmp_path) is None
    assert workspace_path_not_found_error(outside, tmp_path) is None


def test_resolve_path_raises_explicit_path_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    with pytest.raises(PathNotFoundError) as raised:
        resolve_path("missing.txt", must_exist=True)

    assert raised.value.path == tmp_path / "missing.txt"


@pytest.mark.asyncio
async def test_bounded_shell_reports_missing_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = "workgate-missing-shell-issue-106"
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", executable)
    clear_settings_cache()

    with pytest.raises(ShellExecutableNotFoundError) as raised:
        await shell_ops._spawn_process("echo ok", str(tmp_path))

    assert raised.value.executable == executable
    assert raised.value.command == "echo ok"
    assert raised.value.cwd == str(tmp_path)


@pytest.mark.asyncio
async def test_bounded_shell_reports_vanished_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_cwd = tmp_path / "vanished"

    async def fail_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise FileNotFoundError(2, "No such file or directory", "/bin/sh")

    monkeypatch.setattr(
        shell_ops, "_effective_shell_executable", lambda: "/bin/sh"
    )
    monkeypatch.setattr(
        shell_ops.shutil,
        "which",
        lambda command, **_: command,
    )
    monkeypatch.setattr(shell_ops.asyncio, "create_subprocess_exec", fail_spawn)

    with pytest.raises(PathNotFoundError) as raised:
        await shell_ops._spawn_process("echo ok", str(missing_cwd))

    assert raised.value.path == missing_cwd


@pytest.mark.asyncio
async def test_conpty_reports_missing_shell_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = "missing-conpty-shell"
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", executable)
    clear_settings_cache()
    monkeypatch.setattr(conpty, "is_available", lambda: True)

    def fail_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise FileNotFoundError(2, "missing", executable)

    monkeypatch.setattr(conpty, "_spawn_pty", fail_spawn)
    terminal_runtime = build_terminal_runtime()
    await terminal_runtime.start()
    try:
        with pytest.raises(ShellExecutableNotFoundError) as raised:
            await conpty.start_shell(
                shell_id="missing-conpty",
                cwd=tmp_path,
                command=None,
            )
    finally:
        await terminal_runtime.aclose()

    assert raised.value.executable == executable
    assert raised.value.command == executable


def test_worker_error_payload_round_trips_typed_failures(
    tmp_path: Path,
) -> None:
    shell_error = ShellExecutableNotFoundError(
        "missing-shell", "echo ok", tmp_path, "[WinError 2]"
    )
    encoded = tool_error_payload(shell_error, workspace_root=tmp_path)

    assert encoded["status"] == "executable_not_found"
    reconstructed = exception_from_tool_error(encoded)
    assert isinstance(reconstructed, ShellExecutableNotFoundError)
    assert reconstructed.executable == "missing-shell"
    assert reconstructed.command == "echo ok"

    path_error = PathNotFoundError(tmp_path / "missing.txt")
    encoded_path = tool_error_payload(path_error, workspace_root=tmp_path)
    reconstructed_path = exception_from_tool_error(encoded_path)
    assert isinstance(reconstructed_path, PathNotFoundError)
    assert reconstructed_path.path == tmp_path / "missing.txt"


@pytest.mark.asyncio
async def test_posix_persistent_shell_preflights_default_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = "missing-tmux-shell"
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", executable)
    clear_settings_cache()
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "authoritative_persistent_shell_ids_execute",
        lambda: _async_value(set()),
    )
    monkeypatch.setattr(shell_ops.shutil, "which", lambda *args, **kwargs: None)

    with pytest.raises(ShellExecutableNotFoundError) as raised:
        await shell_ops.start_persistent_shell_execute(cwd=str(tmp_path))

    assert raised.value.executable == str(tmp_path / executable)
    assert raised.value.cwd == str(tmp_path)


def _tmux_result(*, ok: bool, stderr: str = "") -> CommandResult:
    return CommandResult(
        ok=ok,
        exit_code=0 if ok else 1,
        timed_out=False,
        duration_ms=1,
        cwd=".",
        command="tmux",
        stdout="",
        stderr=stderr,
        truncated=False,
    )


@pytest.mark.asyncio
async def test_tmux_exec_uses_configured_shell_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    shell = tmp_path / "bin" / "custom-shell"
    shell.parent.mkdir()
    shell.write_text("#!/bin/sh\n", encoding="utf-8")
    shell.chmod(0o700)
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", "bin/custom-shell")
    clear_settings_cache()
    monkeypatch.setattr(
        shell_ops,
        "require_tmux",
        lambda: TmuxSelection("/usr/bin/tmux", "system", "tmux"),
    )
    calls: list[tuple[list[str], str, int | None, dict[str, str] | None]] = []

    async def fake_run_exec(
        argv: list[str],
        *,
        cwd: str = ".",
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        calls.append((argv, cwd, timeout_s, env))
        return _tmux_result(ok=True)

    monkeypatch.setattr(shell_ops, "_run_exec", fake_run_exec)

    result = await shell_ops.tmux(
        ["new-session", "-c", str(tmp_path)], timeout_s=5
    )

    assert result.ok is True
    assert calls == [
        (
            ["/usr/bin/tmux", "new-session", "-c", str(tmp_path)],
            ".",
            5,
            {"TMUX": "", "TMUX_PANE": "", "SHELL": str(shell)},
        )
    ]


@pytest.mark.asyncio
async def test_persistent_tmux_default_shell_is_verified_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    shell = tmp_path / "custom-shell"
    shell.write_text("#!/bin/sh\n", encoding="utf-8")
    shell.chmod(0o700)
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", str(shell))
    clear_settings_cache()
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "authoritative_persistent_shell_ids_execute",
        lambda: _async_value(set()),
    )
    monkeypatch.setattr(
        shell_ops.shutil, "which", lambda executable, **_kwargs: executable
    )
    calls: list[tuple[list[str], int]] = []

    async def fake_tmux(args: list[str], timeout_s: int = 10) -> CommandResult:
        calls.append((args, timeout_s))
        return _tmux_result(ok=True)

    monkeypatch.setattr(shell_ops, "tmux", fake_tmux)
    owner_session_id = (
        get_tool_session_store().create_session(workdir=tmp_path).session_id
    )

    started = await shell_ops.start_persistent_shell_execute(
        cwd=str(tmp_path),
        name="configured-shell",
        owner_session_id=owner_session_id,
    )

    assert started.command == str(shell)
    assert calls == [
        (
            [
                "new-session",
                "-d",
                "-s",
                "configured-shell",
                "-c",
                str(tmp_path),
                ";",
                "set-option",
                "-t",
                "configured-shell",
                "@workgate-session-id",
                owner_session_id,
            ],
            10,
        ),
        (["has-session", "-t", "=configured-shell"], 5),
    ]


@pytest.mark.asyncio
async def test_persistent_tmux_rejects_default_shell_that_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    shell = tmp_path / "exiting-shell"
    shell.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shell.chmod(0o700)
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", str(shell))
    clear_settings_cache()
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "authoritative_persistent_shell_ids_execute",
        lambda: _async_value(set()),
    )
    monkeypatch.setattr(
        shell_ops.shutil, "which", lambda executable, **_kwargs: executable
    )
    calls: list[list[str]] = []

    async def fake_tmux(args: list[str], timeout_s: int = 10) -> CommandResult:
        del timeout_s
        calls.append(args)
        if args[0] == "has-session":
            return _tmux_result(ok=False, stderr="session vanished")
        return _tmux_result(ok=True)

    monkeypatch.setattr(shell_ops, "tmux", fake_tmux)

    with pytest.raises(RuntimeError, match="exited during startup"):
        await shell_ops.start_persistent_shell_execute(
            cwd=str(tmp_path), name="exiting-shell"
        )

    assert calls[-1] == ["kill-session", "-t", "=exiting-shell"]


async def _async_value(value):  # noqa: ANN001, ANN202
    return value


@pytest.mark.asyncio
async def test_manager_preserves_structured_worker_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "true")
    clear_settings_cache()
    manager = RemoteManager()
    manager._load_registry_unlocked()
    worker = RemoteWorker(name="worker", token="token", last_seen=_utc())
    manager.workers[worker.name] = worker
    manager.tokens[worker.token] = worker.name
    call = asyncio.create_task(
        manager.call("worker", "bash", {"command": "echo ok"}, timeout_s=10)
    )
    while worker.queue.qsize() == 0:
        await asyncio.sleep(0)
    job = worker.queue.get_nowait()
    data = {
        "status": "executable_not_found",
        "error_type": "FileNotFoundError",
        "message": "Shell executable not found: missing-shell",
        "executable": "missing-shell",
        "command": "echo ok",
        "cwd": str(tmp_path),
        "original_error": "[WinError 2]",
    }

    accepted = await manager.submit_result(
        "token",
        {
            "job_id": job["id"],
            "ok": False,
            "error": "FileNotFoundError",
            "message": data["message"],
            "data": data,
        },
    )
    result = await call

    assert accepted == {"accepted": True}
    assert result == {"ok": True, "message": "", "data": data}


def test_worker_serializer_and_controller_facade_preserve_error_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    exc = ShellExecutableNotFoundError(
        "missing-shell", "echo ok", tmp_path, "[WinError 2]"
    )

    result = _handled_remote_exception(exc)

    assert result["ok"] is False
    assert result["data"]["status"] == "executable_not_found"
    with pytest.raises(ShellExecutableNotFoundError):
        _remote_result_data(
            {"ok": True, "data": result["data"]},
            tool="bash",
            machine="worker-a",
        )


def test_trimmed_worker_bundle_contains_shared_error_module() -> None:
    with tarfile.open(
        fileobj=io.BytesIO(worker_bundle_bytes()), mode="r:gz"
    ) as archive:
        names = set(archive.getnames())

    assert "workgate/errors.py" in names


def test_http_shell_error_is_not_misreported_as_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = "missing-http-shell"
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", executable)
    clear_settings_cache()

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    response = client.post(
        "/tools/bash",
        json={
            "session_id": session["session_id"],
            "command": "echo ok",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "FileNotFoundError"
    assert response.json()["message"] == (
        f"FileNotFoundError: Shell executable not found: {executable}"
    )
    assert str(tmp_path) not in response.json()["message"]


@pytest.mark.asyncio
async def test_mcp_shell_error_uses_standard_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = "missing-mcp-shell"
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", executable)
    clear_settings_cache()
    session = await session_start_execute(".", "local", None, None)

    with pytest.raises(
        ToolError, match=f"Shell executable not found: {executable}"
    ):
        await build_mcp().call_tool(
            "bash",
            {"session_id": session.session_id, "command": "echo ok"},
        )


@pytest.mark.asyncio
async def test_direct_exec_rejects_empty_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="argv must not be empty"):
        await shell_ops._run_exec([])


@pytest.mark.asyncio
async def test_direct_exec_classifies_missing_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = str(tmp_path / "missing-direct-exec")

    async def fail_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise FileNotFoundError(2, "missing", executable)

    monkeypatch.setattr(shell_ops.asyncio, "create_subprocess_exec", fail_spawn)

    with pytest.raises(ShellExecutableNotFoundError) as raised:
        await shell_ops._spawn_exec_process([executable], str(tmp_path))

    assert raised.value.executable == executable


@pytest.mark.asyncio
async def test_direct_exec_times_out_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    class BlockingSemaphore:
        async def acquire(self) -> None:
            await asyncio.Event().wait()

        def release(self) -> None:
            raise AssertionError("unacquired semaphore must not be released")

    monkeypatch.setattr(shell_ops, "_command_semaphore", BlockingSemaphore)
    monkeypatch.setattr(shell_ops, "clamp_timeout", lambda _timeout: 0.01)

    result = await shell_ops._run_exec(["unused"], cwd=str(tmp_path))

    assert result.timed_out is True
    assert result.exit_code is None
    assert "Timed out while starting subprocess" in result.stderr


@pytest.mark.asyncio
async def test_direct_exec_terminates_running_process_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    class FakeProcess:
        stdout = None
        stderr = None
        returncode: int | None = None
        pid = 123

        async def wait(self) -> None:
            await asyncio.Event().wait()

    process = FakeProcess()

    async def fake_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        return process

    async def fake_terminate(candidate) -> str:  # noqa: ANN001
        assert candidate is process
        process.returncode = -15
        return "forced stop"

    monkeypatch.setattr(shell_ops, "_spawn_exec_process", fake_spawn)
    monkeypatch.setattr(shell_ops, "_terminate_process_group", fake_terminate)
    monkeypatch.setattr(shell_ops, "clamp_timeout", lambda _timeout: 0.01)

    result = await shell_ops._run_exec(["fake"], cwd=str(tmp_path))

    assert result.timed_out is True
    assert result.exit_code == -15
    assert "forced stop" in result.stderr


def test_tmux_cwd_and_absolute_shell_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    absolute = str(tmp_path / "missing-absolute-shell")
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", absolute)
    clear_settings_cache()
    monkeypatch.setattr(shell_ops.shutil, "which", lambda *args, **kwargs: None)

    assert shell_ops._tmux_session_cwd(["new-session"]) == "."
    assert shell_ops._tmux_session_cwd(["list-sessions"]) == "."
    assert shell_ops._resolved_tmux_shell(str(tmp_path)) == absolute


@pytest.mark.asyncio
async def test_persistent_shell_enforces_capacity_and_conpty_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_MAX_TMUX_SESSIONS", "1")
    clear_settings_cache()
    monkeypatch.setattr(
        shell_ops,
        "authoritative_persistent_shell_ids_execute",
        lambda: _async_value({"busy"}),
    )

    with pytest.raises(RuntimeError, match="more than 1"):
        await shell_ops.start_persistent_shell_execute(cwd=str(tmp_path))

    monkeypatch.setattr(
        shell_ops,
        "authoritative_persistent_shell_ids_execute",
        lambda: _async_value(set()),
    )
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: True
    )
    monkeypatch.setattr(conpty, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="pywinpty is required"):
        await shell_ops.start_persistent_shell_execute(cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_persistent_shell_rejects_uncertain_inventory_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    async def uncertain_inventory() -> None:
        return None

    async def unexpected_tmux(*_args, **_kwargs):
        raise AssertionError(
            "new-session must not run without authoritative inventory"
        )

    monkeypatch.setattr(
        shell_ops,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )
    monkeypatch.setattr(shell_ops, "tmux", unexpected_tmux)

    with pytest.raises(RuntimeError, match="inventory is unavailable"):
        await shell_ops.start_persistent_shell_execute(cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_persistent_shell_accepts_missing_tmux_socket_as_empty_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    shell = tmp_path / "shell"
    shell.write_text("#!/bin/sh\n", encoding="utf-8")
    shell.chmod(0o700)
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", str(shell))
    clear_settings_cache()
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "resolve_tmux",
        lambda: TmuxSelection("/usr/bin/tmux", "system", "tmux"),
    )
    monkeypatch.setattr(
        shell_ops.shutil, "which", lambda executable, **_kwargs: executable
    )
    calls: list[list[str]] = []

    async def fake_tmux(args: list[str], timeout_s: int = 10) -> CommandResult:
        del timeout_s
        calls.append(args)
        if args[0] == "list-sessions":
            return _tmux_result(
                ok=False,
                stderr=(
                    "error connecting to /tmp/isolated/tmux-1000/default "
                    "(No such file or directory)"
                ),
            )
        return _tmux_result(ok=True)

    monkeypatch.setattr(shell_ops, "tmux", fake_tmux)

    started = await shell_ops.start_persistent_shell_execute(
        cwd=str(tmp_path), name="fresh", command="echo ready"
    )

    assert started.shell_id == "fresh"
    assert calls[0][0] == "list-sessions"
    assert calls[1][:4] == ["new-session", "-d", "-s", "fresh"]


@pytest.mark.asyncio
async def test_persistent_tmux_creation_and_send_errors_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    shell = tmp_path / "shell"
    shell.write_text("#!/bin/sh\n", encoding="utf-8")
    shell.chmod(0o700)
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", str(shell))
    clear_settings_cache()
    monkeypatch.setattr(
        shell_ops, "_use_conpty_persistent_shell_backend", lambda: False
    )
    monkeypatch.setattr(
        shell_ops,
        "authoritative_persistent_shell_ids_execute",
        lambda: _async_value(set()),
    )

    monkeypatch.setattr(
        shell_ops.shutil, "which", lambda executable, **_kwargs: executable
    )

    async def failed_tmux(
        args: list[str], timeout_s: int = 10
    ) -> CommandResult:
        del timeout_s
        if args[0] == "kill-session":
            return _tmux_result(
                ok=False,
                stderr="no server running on /tmp/tmux/default",
            )
        return _tmux_result(ok=False, stderr=f"failed {args[0]}")

    monkeypatch.setattr(shell_ops, "tmux", failed_tmux)
    with pytest.raises(RuntimeError, match="failed new-session"):
        await shell_ops.start_persistent_shell_execute(
            cwd=str(tmp_path), command="echo ok"
        )
    with pytest.raises(RuntimeError, match="failed send-keys"):
        await shell_ops.send_persistent_shell_input_execute(
            "shell-1", "echo ok", enter=False
        )
    with pytest.raises(RuntimeError, match="failed send-keys"):
        await shell_ops.send_persistent_shell_input_execute(
            "shell-1", "", enter=True
        )


@pytest.mark.asyncio
async def test_direct_exec_uses_windows_process_group_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    expected = object()

    async def fake_create(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["args"] = args
        captured["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(
        shell_ops,
        "new_process_group_kwargs",
        lambda: {"creationflags": 512},
    )
    monkeypatch.setattr(
        shell_ops.asyncio, "create_subprocess_exec", fake_create
    )

    result = await shell_ops._spawn_exec_process(
        ["tmux.exe", "list-sessions"], str(tmp_path), {"SHELL": "cmd.exe"}
    )

    assert result is expected
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["creationflags"] == 512
    assert "start_new_session" not in kwargs


@pytest.mark.asyncio
async def test_direct_exec_cancellation_terminates_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    spawned = asyncio.Event()
    terminated = asyncio.Event()

    class FakeProcess:
        stdout = None
        stderr = None
        returncode: int | None = None
        pid = 321

        async def wait(self) -> None:
            spawned.set()
            await asyncio.Event().wait()

    process = FakeProcess()

    async def fake_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        return process

    async def fake_terminate(candidate) -> str:  # noqa: ANN001
        assert candidate is process
        process.returncode = -15
        terminated.set()
        return ""

    monkeypatch.setattr(shell_ops, "_spawn_exec_process", fake_spawn)
    monkeypatch.setattr(shell_ops, "_terminate_process_group", fake_terminate)
    task = asyncio.create_task(
        shell_ops._run_exec(["fake"], cwd=str(tmp_path), timeout_s=10)
    )
    await asyncio.wait_for(spawned.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert terminated.is_set()
