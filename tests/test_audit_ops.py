from typing import Any

import pytest

import workgate.tools.ops.audit as audit_ops
from workgate.audit import (
    audit,
    audit_call_context,
    audit_tool_call_end,
    audit_tool_call_start,
)
from workgate.config.settings import clear_settings_cache
from workgate.tool_session.store import get_tool_session_store
from workgate.tools.ops.audit import audit_tail_execute


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    get_tool_session_store().clear()
    yield
    get_tool_session_store().clear()
    clear_settings_cache()


@pytest.mark.asyncio
async def test_audit_tail_local_query_excludes_only_current_call(tmp_path):
    session = get_tool_session_store().create_session(workdir=tmp_path)
    audit_tool_call_start(
        call_id="older-audit-tail",
        transport="mcp",
        tool="audit_tail",
        input={"session_id": session.session_id},
    )
    audit_tool_call_end(
        call_id="older-audit-tail",
        transport="mcp",
        tool="audit_tail",
        ok=True,
        duration_ms=1,
        output={"count": 0},
    )
    audit_tool_call_start(
        call_id="current-audit-tail",
        transport="mcp",
        tool="audit_tail",
        input={"session_id": session.session_id},
    )

    with audit_call_context("current-audit-tail"):
        result = await audit_tail_execute(session.session_id, limit=100)

    ids = {entry["id"] for entry in result.entries}
    assert "call:older-audit-tail" in ids
    assert "call:current-audit-tail" not in ids
    assert result.session_id == session.session_id
    assert result.target == "local"
    assert result.machine is None


@pytest.mark.asyncio
async def test_audit_tail_local_detail_resolves_sanitized_payload(tmp_path):
    session = get_tool_session_store().create_session(workdir=tmp_path)
    audit(
        "large-local-entry",
        payload={"token": "private-token-value", "body": "value-" * 4_000},
    )
    listing = await audit_tail_execute(
        session.session_id, event="large-local-entry"
    )
    entry_id = listing.entries[0]["id"]

    detail = await audit_tail_execute(
        session.session_id,
        entry_id=entry_id,
        include_full_payloads=True,
    )

    assert detail.full_payloads is True
    assert detail.entry_id == entry_id
    assert detail.entries[0]["payload"] == {
        "token": "<redacted>",
        "body": "value-" * 4_000,
    }
    with pytest.raises(ValueError, match="requires entry_id"):
        await audit_tail_execute(session.session_id, include_full_payloads=True)


@pytest.mark.asyncio
async def test_audit_tail_remote_maps_session_filter_and_forwards_detail(
    monkeypatch,
):
    session = get_tool_session_store().create_session(
        target="remote",
        machine="edge",
        worker_session_id="worker123",
        workdir="/srv/project",
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(
        _session: Any, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        calls.append((tool, dict(args)))
        if tool == "query_audit":
            return {
                "entries": [
                    {
                        "id": "call:worker-read",
                        "session": "worker123",
                        "status": "success",
                    }
                ],
                "count": 1,
                "total_matched": 1,
                "failed_matched": 0,
            }
        assert tool == "get_audit_entry"
        return {
            "id": "call:worker-read",
            "session_id": session.session_id,
            "status": "success",
            "output": {"body": "remote-full"},
        }

    monkeypatch.setattr(audit_ops, "call_remote_session_tool", fake_call)

    listing = await audit_tail_execute(
        session.session_id,
        audit_session=session.session_id,
        operation="files",
    )
    detail = await audit_tail_execute(
        session.session_id,
        entry_id="call:worker-read",
        include_full_payloads=True,
    )

    assert listing.target == "remote"
    assert listing.machine == "edge"
    assert listing.entries[0]["session"] == "worker123"
    assert detail.entries[0]["output"] == {"body": "remote-full"}
    assert calls == [
        (
            "query_audit",
            {
                "limit": 100,
                "event": None,
                "operation": "files",
                "session": "worker123",
                "search": None,
                "start_ts": None,
                "end_ts": None,
                "sort": "desc",
            },
        ),
        (
            "get_audit_entry",
            {"id": "call:worker-read", "include_full_payloads": True},
        ),
    ]


def test_http_audit_tail_enforces_read_and_full_scopes(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from workgate.control.http.app import build_http_app
    from workgate.oauth.core.scopes import (
        SCOPE_AUDIT_FULL,
        SCOPE_AUDIT_READ,
    )
    from workgate.oauth.protocol.token_codec import issue_access_token

    base_url = "https://audit-tool.example"
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", base_url)
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()

    def headers(scope: str) -> dict[str, str]:
        token = issue_access_token(
            client_id="audit-tail-test",
            scope=scope,
            resource=f"{base_url}/mcp",
        )
        return {"Authorization": f"Bearer {token}"}

    client = TestClient(build_http_app(), base_url=base_url)
    session = get_tool_session_store().create_session(workdir=tmp_path)
    audit(
        "route-large-entry",
        payload={"token": "route-secret", "body": "route-" * 4_000},
    )

    denied_read = client.get(
        "/tools/audit_tail",
        params={"session_id": session.session_id},
        headers=headers("shell:read"),
    )
    listing = client.get(
        "/tools/audit_tail",
        params={
            "session_id": session.session_id,
            "event": "route-large-entry",
        },
        headers=headers(SCOPE_AUDIT_READ),
    )
    entry_id = listing.json()["entries"][0]["id"]
    denied_full = client.get(
        "/tools/audit_tail",
        params={
            "session_id": session.session_id,
            "entry_id": entry_id,
            "include_full_payloads": "true",
        },
        headers=headers(SCOPE_AUDIT_READ),
    )
    full = client.get(
        "/tools/audit_tail",
        params={
            "session_id": session.session_id,
            "entry_id": entry_id,
            "include_full_payloads": "true",
        },
        headers=headers(f"{SCOPE_AUDIT_READ} {SCOPE_AUDIT_FULL}"),
    )

    assert denied_read.status_code == 403
    assert SCOPE_AUDIT_READ in denied_read.text
    assert listing.status_code == 200
    assert denied_full.status_code == 403
    assert SCOPE_AUDIT_FULL in denied_full.text
    assert full.status_code == 200
    assert full.json()["entries"][0]["payload"] == {
        "token": "<redacted>",
        "body": "route-" * 4_000,
    }
