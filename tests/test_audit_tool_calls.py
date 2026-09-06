import json

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from tests.helpers import mcp_text
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp


def _audit_records(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _tool_call_pairs(records, tool_name):
    starts = [
        r
        for r in records
        if r.get("event") == "tool_call_start" and r.get("tool") == tool_name
    ]
    ends = [
        r
        for r in records
        if r.get("event") == "tool_call_end" and r.get("tool") == tool_name
    ]
    return starts, ends


def test_http_tool_calls_audit_full_input_output_and_auth_context(
    tmp_path, monkeypatch
):
    (tmp_path / "alpha.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    response = client.post(
        "/tools/read",
        json={"session_id": session["session_id"], "path": "alpha.txt"},
    )

    assert response.status_code == 200
    records = _audit_records(get_settings().audit_log_path)
    starts, ends = _tool_call_pairs(records, "read")

    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0]["call_id"] == ends[0]["call_id"]
    assert starts[0]["transport"] == "http"
    assert starts[0]["input"] == {
        "session_id": session["session_id"],
        "path": "alpha.txt",
    }
    assert ends[0]["ok"] is True
    assert ends[0]["output"] == response.json()
    assert ends[0]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_mcp_tool_calls_audit_full_input_output(tmp_path, monkeypatch):
    (tmp_path / "beta.txt").write_text("world", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    session = json.loads(
        mcp_text(await build_mcp().call_tool("session_start", {"workdir": "."}))
    )
    response = await build_mcp().call_tool(
        "read",
        {"session_id": session["session_id"], "path": "beta.txt:raw"},
    )
    payload = json.loads(mcp_text(response))

    records = _audit_records(get_settings().audit_log_path)
    starts, ends = _tool_call_pairs(records, "read")

    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0]["call_id"] == ends[0]["call_id"]
    assert starts[0]["transport"] == "mcp"
    assert starts[0]["input"]["session_id"] == session["session_id"]
    assert starts[0]["input"]["path"] == "beta.txt:raw"
    assert "binary_preview_bytes" not in starts[0]["input"]
    assert ends[0]["ok"] is True
    assert ends[0]["output"] == payload


@pytest.mark.asyncio
async def test_mcp_tool_structured_errors_are_audited_with_input_and_output(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    session = json.loads(
        mcp_text(await build_mcp().call_tool("session_start", {"workdir": "."}))
    )
    with pytest.raises(ToolError, match="Error executing tool read"):
        await build_mcp().call_tool(
            "read",
            {"session_id": session["session_id"], "path": "missing.txt"},
        )

    records = _audit_records(get_settings().audit_log_path)
    starts, ends = _tool_call_pairs(records, "read")

    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0]["input"]["session_id"] == session["session_id"]
    assert starts[0]["input"]["path"] == "missing.txt"
    assert "binary_preview_bytes" not in starts[0]["input"]
    assert ends[0]["ok"] is False
    assert ends[0]["error"]["type"] == "FileNotFoundError"
    assert "missing.txt" in ends[0]["error"]["message"]
