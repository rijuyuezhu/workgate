import asyncio

import pytest

from tests.helpers import (
    build_paired_http_app,
    build_paired_mcp,
    mcp_structured,
)
from workgate.audit import (
    audit,
    audit_call_context,
    audit_tool_call_end,
    audit_tool_call_start,
    audit_tool_input_task_ids,
)
from workgate.config.settings import clear_settings_cache, get_settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()
    yield
    clear_settings_cache()


async def _task_session(tmp_path):
    mcp, harness = build_paired_mcp(get_settings())
    task_result = mcp_structured(
        await mcp.call_tool("task", {"action": "create", "label": "audit task"})
    )
    task = task_result.get("result", task_result)
    session = mcp_structured(
        await mcp.call_tool(
            "session_start",
            {"workdir": str(tmp_path), "task_id": task["task_id"]},
        )
    )
    return str(task["task_id"]), str(session["session_id"]), harness


@pytest.mark.asyncio
async def test_control_audit_query_excludes_only_current_call(tmp_path):
    task_id, session_id, harness = await _task_session(tmp_path)
    older_input = {"task_id": task_id}
    older_sessions = audit_tool_call_start(
        call_id="older-audit-tail",
        transport="mcp",
        tool="audit_tail",
        input=older_input,
    )
    older_tasks = audit_tool_input_task_ids(older_input, older_sessions)
    audit_tool_call_end(
        call_id="older-audit-tail",
        transport="mcp",
        tool="audit_tail",
        ok=True,
        duration_ms=1,
        output={"count": 0},
        task_ids=older_tasks,
    )
    current_input = {"task_id": task_id}
    current_sessions = audit_tool_call_start(
        call_id="current-audit-tail",
        transport="mcp",
        tool="audit_tail",
        input=current_input,
    )
    current_tasks = audit_tool_input_task_ids(current_input, current_sessions)

    with audit_call_context(
        "current-audit-tail", current_sessions, current_tasks
    ):
        result = await harness.control.audit_service.execute(
            task_id=task_id, limit=100
        )

    ids = {entry["id"] for entry in result.entries}
    assert "call:older-audit-tail" in ids
    assert "call:current-audit-tail" not in ids
    assert result.task_id == task_id
    # A task-wide query still includes records attributed to attached execution
    # sessions; only the audit call currently performing the query is excluded.
    assert any(entry.get("session") == session_id for entry in result.entries)


@pytest.mark.asyncio
async def test_control_audit_is_task_wide_with_session_attribution(tmp_path):
    task_id, session_id, harness = await _task_session(tmp_path)
    audit(
        "canonical-control-entry",
        session_id=session_id,
        operation="files",
        payload={"token": "private-token-value", "body": "visible"},
    )

    result = await harness.control.audit_service.execute(
        task_id=task_id,
        event="canonical-control-entry",
    )
    session_only = await harness.control.audit_service.execute(
        task_id=task_id,
        session_id=session_id,
        event="canonical-control-entry",
    )

    assert result.count == 1
    assert result.entries[0]["event"] == "canonical-control-entry"
    assert result.entries[0]["task"] == task_id
    assert result.entries[0]["session"] == session_id
    assert result.entries[0]["payload"]["token"] == "<redacted>"
    assert session_only.count == 1


@pytest.mark.asyncio
async def test_control_audit_can_query_unattached_ended_session(tmp_path):
    _mcp, harness = build_paired_mcp(get_settings())
    started = await harness.control.session_coordinator.start_session(
        workdir=str(tmp_path), executor_id=harness.executor_id
    )
    assert isinstance(started, dict)
    session_id = str(started["session_id"])
    audit("session-only-entry", session_id=session_id)
    await harness.control.session_coordinator.end_session(session_id)

    result = await harness.control.audit_service.execute(
        session_id=session_id,
        event="session-only-entry",
    )

    assert result.task_id is None
    assert result.session_id == session_id
    assert result.count == 1
    assert result.entries[0]["session"] == session_id


@pytest.mark.asyncio
async def test_cross_task_tool_call_is_visible_from_both_tasks_and_sessions(
    tmp_path,
):
    first_task, first_session, harness = await _task_session(tmp_path)
    second = await harness.control.task_service.create_task(label="second task")
    started = await harness.control.session_coordinator.start_session(
        workdir=str(tmp_path),
        executor_id=harness.executor_id,
        task_id=second.task_id,
    )
    assert isinstance(started, dict)
    second_session = str(started["session_id"])
    payload = {
        "src_session_id": first_session,
        "dst_session_id": second_session,
    }
    session_ids = audit_tool_call_start(
        call_id="cross-task-copy",
        transport="mcp",
        tool="session_copy",
        input=payload,
    )
    task_ids = audit_tool_input_task_ids(payload, session_ids)
    audit_tool_call_end(
        call_id="cross-task-copy",
        transport="mcp",
        tool="session_copy",
        ok=True,
        duration_ms=1,
        output={"copied": True},
        session_ids=session_ids,
        task_ids=task_ids,
    )

    for task_id in (first_task, second.task_id):
        result = await harness.control.audit_service.execute(task_id=task_id)
        entry = next(
            item
            for item in result.entries
            if item.get("tool") == "session_copy"
        )
        assert set(entry["task_ids"]) == {first_task, second.task_id}
        assert set(entry["session_ids"]) == {first_session, second_session}

    for session_id in (first_session, second_session):
        result = await harness.control.audit_service.execute(
            session_id=session_id
        )
        assert any(
            item.get("tool") == "session_copy" for item in result.entries
        )


@pytest.mark.asyncio
async def test_migrated_legacy_audit_entry_remains_readable_by_task(tmp_path):
    _mcp, harness = build_paired_mcp(get_settings())
    started = await harness.control.session_coordinator.start_session(
        workdir=str(tmp_path), executor_id=harness.executor_id
    )
    assert isinstance(started, dict)
    session_id = str(started["session_id"])

    audit("legacy-before-task-identity", session_id=session_id)
    legacy = harness.control.state_store.layout.control_task_state_path(
        session_id
    )
    harness.control.state_store.write_json(
        legacy,
        {
            "revision": 0,
            "updated_at": 1.0,
            "todos": [],
        },
    )
    assert await harness.control.task_service.migrate_legacy_sessions() == 1
    record = harness.control.control_state.snapshot_sessions()[session_id]
    assert record.task_id is not None
    task_id = str(record.task_id)

    listing = await harness.control.audit_service.execute(
        task_id=task_id,
        event="legacy-before-task-identity",
    )
    assert listing.count == 1
    assert listing.entries[0].get("task") is None
    assert listing.entries[0]["session"] == session_id

    detail = await harness.control.audit_service.execute(
        task_id=task_id,
        entry_id=str(listing.entries[0]["id"]),
    )
    assert detail.count == 1
    assert detail.entries[0]["event"] == "legacy-before-task-identity"


@pytest.mark.asyncio
async def test_task_audit_rejects_unattached_session_filter(tmp_path):
    task_id, _session_id, harness = await _task_session(tmp_path)
    other = await harness.control.session_coordinator.start_session(
        workdir=str(tmp_path), executor_id=harness.executor_id
    )
    assert isinstance(other, dict)

    with pytest.raises(ValueError, match="is not attached to task"):
        await harness.control.audit_service.execute(
            task_id=task_id,
            session_id=str(other["session_id"]),
        )


def test_http_audit_tail_enforces_read_and_full_scopes(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from workgate.oauth.core.scopes import SCOPE_AUDIT_FULL, SCOPE_AUDIT_READ
    from workgate.oauth.protocol.token_codec import issue_access_token

    base_url = "https://audit-tool.example"
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", base_url)
    clear_settings_cache()

    app, harness = build_paired_http_app(get_settings())
    task = asyncio.run(
        harness.control.task_service.create_task(label="http audit")
    )
    session = asyncio.run(
        harness.control.session_coordinator.start_session(
            workdir=str(tmp_path),
            task_id=task.task_id,
        )
    )
    assert isinstance(session, dict)
    session_id = str(session["session_id"])

    def headers(scope: str) -> dict[str, str]:
        token = issue_access_token(
            client_id="audit-tail-test",
            scope=scope,
            resource=f"{base_url}/mcp",
        )
        return {"Authorization": f"Bearer {token}"}

    audit(
        "route-large-entry",
        session_id=session_id,
        payload={"token": "route-secret", "body": "route-" * 4_000},
    )
    client = TestClient(app, base_url=base_url)

    denied_read = client.get(
        "/tools/audit_tail",
        params={"task_id": task.task_id},
        headers=headers("shell:read"),
    )
    listing = client.get(
        "/tools/audit_tail",
        params={"task_id": task.task_id, "event": "route-large-entry"},
        headers=headers(SCOPE_AUDIT_READ),
    )
    entry_id = listing.json()["entries"][0]["id"]
    denied_full = client.get(
        "/tools/audit_tail",
        params={
            "task_id": task.task_id,
            "entry_id": entry_id,
            "include_full_payloads": "true",
        },
        headers=headers(SCOPE_AUDIT_READ),
    )
    full = client.get(
        "/tools/audit_tail",
        params={
            "task_id": task.task_id,
            "entry_id": entry_id,
            "include_full_payloads": "true",
        },
        headers=headers(f"{SCOPE_AUDIT_READ} {SCOPE_AUDIT_FULL}"),
    )

    assert denied_read.status_code == 403
    assert listing.status_code == 200
    assert denied_full.status_code == 403
    assert full.status_code == 200
    assert full.json()["entries"][0]["payload"] == {
        "token": "<redacted>",
        "body": "route-" * 4_000,
    }
