import gzip
import json

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from tests.helpers import build_paired_http_app, build_paired_mcp, mcp_text
from workgate.config.settings import clear_settings_cache, get_settings


def _audit_records(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _tool_call_pairs(records, tool_name, *, transport):
    starts = [
        r
        for r in records
        if r.get("event") == "tool_call_start"
        and r.get("tool") == tool_name
        and r.get("transport") == transport
    ]
    ends = [
        r
        for r in records
        if r.get("event") == "tool_call_end"
        and r.get("tool") == tool_name
        and r.get("transport") == transport
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

    app, _harness = build_paired_http_app(get_settings())
    client = TestClient(app)
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    response = client.post(
        "/tools/read",
        json={"session_id": session["session_id"], "path": "alpha.txt"},
    )

    assert response.status_code == 200
    records = _audit_records(get_settings().audit_log_path)
    starts, ends = _tool_call_pairs(records, "read", transport="http")

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


def test_task_tool_audit_redacts_durable_report_and_plan_prose(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    settings = get_settings()
    app, _harness = build_paired_http_app(settings)
    client = TestClient(app)
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    session_id = session["session_id"]
    marker = "task-audit-secret-marker"
    long_content = marker + "-" + ("x" * 6_000)

    reported = client.post(
        "/tools/session-progress",
        json={
            "session_id": session_id,
            "expected_revision": 0,
            "objective": f"{marker}-objective",
            "summary": f"{marker}-summary",
            "findings": [f"{marker}-finding"],
            "next_action": f"{marker}-next",
            "blockers": [f"{marker}-blocker"],
        },
    )
    assert reported.status_code == 200
    planned = client.post(
        "/tools/session-plan",
        json={
            "session_id": session_id,
            "expected_revision": 1,
            "steps": [
                {
                    "id": "step-1",
                    "content": long_content,
                    "status": "in_progress",
                    "priority": "high",
                    "note": f"{marker}-note",
                }
            ],
        },
    )
    assert planned.status_code == 200
    rejected_extra = client.post(
        "/tools/session-plan",
        json={
            "session_id": session_id,
            "expected_revision": 2,
            "steps": [
                {
                    "id": "step-1",
                    "content": "unchanged",
                    "private_context": f"{marker}-extra",
                }
            ],
        },
    )
    assert rejected_extra.status_code == 400
    assert (
        client.get(
            "/tools/session-task", params={"session_id": session_id}
        ).status_code
        == 200
    )
    assert (
        client.get("/tools/todo", params={"session_id": session_id}).status_code
        == 200
    )
    compat = client.post(
        "/tools/todo",
        json={
            "session_id": session_id,
            "expected_revision": 2,
            "todos": [
                {
                    "id": "step-1",
                    "content": f"{marker}-compat-content",
                    "status": "completed",
                    "priority": "high",
                }
            ],
        },
    )
    assert compat.status_code == 200

    audit_text = settings.audit_log_path.read_text(encoding="utf-8")
    assert marker not in audit_text
    for payload_path in settings.audit_payload_dir.glob("*.json.gz"):
        with gzip.open(payload_path, "rt", encoding="utf-8") as payload_file:
            assert marker not in payload_file.read()

    records = _audit_records(settings.audit_log_path)
    report_starts, report_ends = _tool_call_pairs(
        records, "report_session_progress", transport="http"
    )
    assert report_starts[0]["input"]["session_id"] == session_id
    assert report_starts[0]["input"]["objective"] == "<redacted>"
    assert report_starts[0]["input"]["findings"] == "<redacted>"
    assert report_ends[0]["output"]["objective"] == "<redacted>"
    assert report_ends[0]["output"]["progress"]["summary"] == "<redacted>"

    plan_starts, plan_ends = _tool_call_pairs(
        records, "update_session_plan", transport="http"
    )
    assert plan_starts[0]["input"]["steps"][0]["id"] == "step-1"
    assert plan_starts[0]["input"]["steps"][0]["content"] == "<redacted>"
    assert plan_starts[0]["input"]["steps"][0]["note"] == "<redacted>"
    assert len(plan_starts) == 2
    assert len(plan_ends) == 2
    assert (
        plan_starts[1]["input"]["steps"][0]["private_context"] == "<redacted>"
    )
    assert plan_ends[0]["output"]["plan"]["steps"][0]["id"] == "step-1"
    assert plan_ends[0]["output"]["plan"]["steps"][0]["content"] == "<redacted>"
    assert plan_ends[1]["ok"] is False

    todo_starts, todo_ends = _tool_call_pairs(
        records, "write_todos", transport="http"
    )
    assert todo_starts[0]["input"]["todos"][0]["id"] == "step-1"
    assert todo_starts[0]["input"]["todos"][0]["content"] == "<redacted>"
    assert todo_ends[0]["output"]["todos"][0]["content"] == "<redacted>"


@pytest.mark.asyncio
async def test_mcp_tool_calls_audit_full_input_output(tmp_path, monkeypatch):
    (tmp_path / "beta.txt").write_text("world", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    mcp, _harness = build_paired_mcp(get_settings())
    session = json.loads(
        mcp_text(await mcp.call_tool("session_start", {"workdir": "."}))
    )
    response = await mcp.call_tool(
        "read",
        {"session_id": session["session_id"], "path": "beta.txt:raw"},
    )
    payload = json.loads(mcp_text(response))

    records = _audit_records(get_settings().audit_log_path)
    starts, ends = _tool_call_pairs(records, "read", transport="mcp")

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

    mcp, _harness = build_paired_mcp(get_settings())
    session = json.loads(
        mcp_text(await mcp.call_tool("session_start", {"workdir": "."}))
    )
    with pytest.raises(ToolError, match="Error executing tool read"):
        await mcp.call_tool(
            "read",
            {"session_id": session["session_id"], "path": "missing.txt"},
        )

    records = _audit_records(get_settings().audit_log_path)
    starts, ends = _tool_call_pairs(records, "read", transport="mcp")

    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0]["input"]["session_id"] == session["session_id"]
    assert starts[0]["input"]["path"] == "missing.txt"
    assert "binary_preview_bytes" not in starts[0]["input"]
    assert ends[0]["ok"] is False
    assert ends[0]["error"]["type"] == "FileNotFoundError"
    assert "missing.txt" in ends[0]["error"]["message"]
