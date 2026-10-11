from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from tests.support.paired import build_paired_http_app, build_paired_mcp
from tests.support.responses import mcp_structured
from tests.support.sessions import (
    get_test_tool_session_store as get_tool_session_store,
)
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.errors import (
    PathNotFoundError,
    SessionTerminationRequestedError,
    ShellExecutableNotFoundError,
    exception_from_tool_error,
    process_start_not_found_error,
    syscall_path_not_found_error,
    tool_error_payload,
)
from workgate.executor.path import resolve_path


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    get_tool_session_store().clear()


def _resolve_ambient_path(
    path: str | Path, *, must_exist: bool = False
) -> Path:
    settings = get_settings()
    return resolve_path(
        path,
        base=settings.default_workdir,
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


def test_syscall_path_detection_uses_missing_second_endpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    missing_destination = tmp_path / "removed-parent" / "destination.txt"
    exc = FileNotFoundError(2, "No such file or directory", str(source))
    exc.filename2 = str(missing_destination)

    result = syscall_path_not_found_error(exc)

    assert isinstance(result, PathNotFoundError)
    assert result.path == missing_destination


def test_syscall_path_detection_ignores_relative_endpoint(
    tmp_path: Path,
) -> None:
    relative = FileNotFoundError(2, "missing", "relative.txt")
    assert syscall_path_not_found_error(relative) is None


def test_syscall_path_detection_accepts_absolute_endpoint_outside_default(
    tmp_path: Path,
) -> None:
    outside = FileNotFoundError(
        2, "missing", str(tmp_path.parent / "outside-default")
    )

    result = syscall_path_not_found_error(outside)
    assert isinstance(result, PathNotFoundError)
    assert result.path == tmp_path.parent / "outside-default"


def test_explicit_path_resolution_raises_typed_missing_path_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    with pytest.raises(PathNotFoundError) as raised:
        _resolve_ambient_path("missing.txt", must_exist=True)

    assert raised.value.path == tmp_path / "missing.txt"


def test_tool_error_payload_round_trips_typed_failures(tmp_path: Path) -> None:
    shell_error = ShellExecutableNotFoundError(
        "missing-shell", "echo ok", tmp_path, "No such file or directory"
    )
    encoded = tool_error_payload(shell_error)

    assert encoded["status"] == "executable_not_found"
    reconstructed = exception_from_tool_error(encoded)
    assert isinstance(reconstructed, ShellExecutableNotFoundError)
    assert reconstructed.executable == "missing-shell"
    assert reconstructed.command == "echo ok"

    path_error = PathNotFoundError(tmp_path / "missing.txt")
    encoded_path = tool_error_payload(path_error)
    reconstructed_path = exception_from_tool_error(encoded_path)
    assert isinstance(reconstructed_path, PathNotFoundError)
    assert reconstructed_path.path == tmp_path / "missing.txt"


def test_executor_termination_roundtrip_keeps_model_stop_instruction():
    session_id = "sess_123456789123456789123456789"
    encoded = tool_error_payload(SessionTerminationRequestedError(session_id))
    assert encoded["status"] == "termination_requested"
    assert encoded["session_id"] == session_id
    reconstructed = exception_from_tool_error(encoded)
    assert isinstance(reconstructed, SessionTerminationRequestedError)
    assert reconstructed.session_id == session_id
    assert "Stop immediately" in str(reconstructed)


def test_http_shell_error_is_not_misreported_as_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    executable = "missing-http-shell"
    monkeypatch.setenv("WORKGATE_SHELL_EXECUTABLE", executable)
    clear_settings_cache()

    app, _harness = build_paired_http_app(get_settings())
    client = TestClient(app)
    session = client.post("/tools/session_start", json={"workdir": "."}).json()[
        "structuredContent"
    ]
    response = client.post(
        "/tools/bash",
        json={
            "session_id": session["session_id"],
            "command": "echo ok",
        },
    )

    assert response.status_code == 200
    assert response.json()["isError"] is True
    text = response.json()["content"][0]["text"]
    assert "Shell executable not found" in text
    assert str(tmp_path) not in text


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


def test_process_start_distinguishes_missing_executable_from_existing_cwd(
    tmp_path,
):
    exc = FileNotFoundError(2, "No such file", "/missing/executable")
    classified = process_start_not_found_error(
        exc, executable="missing", command="echo ok", cwd=tmp_path
    )
    assert isinstance(classified, ShellExecutableNotFoundError)
    assert classified.cwd == str(tmp_path)


def test_process_start_prefers_cwd_named_in_syscall_even_when_recreated(
    tmp_path,
):
    exc = FileNotFoundError(2, "No such file", str(tmp_path))
    classified = process_start_not_found_error(
        exc, executable="sh", command="echo ok", cwd=tmp_path
    )
    assert isinstance(classified, PathNotFoundError)
    assert classified.path == tmp_path


def test_raw_missing_path_error_is_classified_for_remote_transfer(tmp_path):
    missing = tmp_path / "not-found.bin"
    exc = FileNotFoundError(2, "No such file", str(missing))
    payload = tool_error_payload(exc)
    assert payload["status"] == "not_found"
    assert payload["path"] == str(missing)


def test_remote_termination_without_session_id_fails_closed():
    with pytest.raises(ValueError, match="requires session_id"):
        exception_from_tool_error({"status": "termination_requested"})
