from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

import workgate.executor.terminal.conpty as conpty
from tests.helpers import (
    build_paired_http_app,
    build_paired_mcp,
    mcp_structured,
)
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.errors import (
    PathNotFoundError,
    ShellExecutableNotFoundError,
    exception_from_tool_error,
    process_start_not_found_error,
    tool_error_payload,
    workspace_path_not_found_error,
)
from workgate.executor.terminal.runtime import build_terminal_runtime
from workgate.persistence import get_state_store
from workgate.tool_session.store import get_tool_session_store
from workgate.utils.path_policy import resolve_path_with_policy


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    get_tool_session_store().clear()


def _resolve_ambient_path(
    path: str | Path, *, must_exist: bool = False
) -> Path:
    settings = get_settings()
    return resolve_path_with_policy(
        path,
        workspace_root=settings.workspace_root,
        allow_full_control=settings.allow_full_control,
        path_denylist=tuple(settings.path_denylist),
        must_exist=must_exist,
    )


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


def test_explicit_path_policy_raises_typed_missing_path_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    with pytest.raises(PathNotFoundError) as raised:
        _resolve_ambient_path("missing.txt", must_exist=True)

    assert raised.value.path == tmp_path / "missing.txt"


def test_tool_error_payload_round_trips_typed_failures(tmp_path: Path) -> None:
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
async def test_conpty_reports_explicit_missing_shell_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = "missing-conpty-shell"
    monkeypatch.setattr(conpty, "is_available", lambda: True)

    def fail_spawn(*_args, **_kwargs):
        raise FileNotFoundError(2, "missing", executable)

    monkeypatch.setattr(conpty, "_spawn_pty", fail_spawn)
    terminal_runtime = build_terminal_runtime(
        get_state_store(), workspace_root=tmp_path
    )
    await terminal_runtime.start()
    try:
        with pytest.raises(ShellExecutableNotFoundError) as raised:
            await conpty.start_shell(
                shell_id="missing-conpty",
                cwd=tmp_path,
                command=None,
                shell_executable=executable,
            )
    finally:
        await terminal_runtime.aclose()

    assert raised.value.executable == executable
    assert raised.value.command == executable


def test_http_shell_error_is_not_misreported_as_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = "missing-http-shell"
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", executable)
    clear_settings_cache()

    app, _harness = build_paired_http_app(get_settings())
    client = TestClient(app)
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
    mcp, _harness = build_paired_mcp(get_settings())
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )

    with pytest.raises(
        ToolError, match=f"Shell executable not found: {executable}"
    ):
        await mcp.call_tool(
            "bash",
            {"session_id": session["session_id"], "command": "echo ok"},
        )
