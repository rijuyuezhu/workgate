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


async def _shared_session(tmp_path):
    mcp, harness = build_paired_mcp(get_settings())
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": str(tmp_path)})
    )
    return str(session["session_id"]), harness


@pytest.mark.asyncio
async def test_control_audit_query_excludes_only_current_call(tmp_path):
    session_id, harness = await _shared_session(tmp_path)
    audit_tool_call_start(
        call_id="older-audit-tail",
        transport="mcp",
        tool="audit_tail",
        input={"session_id": session_id},
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
        input={"session_id": session_id},
    )

    with audit_call_context("current-audit-tail"):
        result = await harness.control.audit_service.execute(
            session_id=session_id, limit=100
        )

    ids = {entry["id"] for entry in result.entries}
    assert "call:older-audit-tail" in ids
    assert "call:current-audit-tail" not in ids
    assert result.session_id == session_id


@pytest.mark.asyncio
async def test_control_audit_is_canonical_across_executor_bound_session(
    tmp_path,
):
    session_id, harness = await _shared_session(tmp_path)
    audit(
        "canonical-control-entry",
        session_id=session_id,
        operation="files",
        payload={"token": "private-token-value", "body": "visible"},
    )

    result = await harness.control.audit_service.execute(
        session_id=session_id,
        event="canonical-control-entry",
        audit_session=session_id,
    )

    assert result.count == 1
    assert result.entries[0]["event"] == "canonical-control-entry"
    assert result.entries[0]["payload"]["token"] == "<redacted>"


def test_http_audit_tail_enforces_read_and_full_scopes(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from workgate.oauth.core.scopes import SCOPE_AUDIT_FULL, SCOPE_AUDIT_READ
    from workgate.oauth.protocol.token_codec import issue_access_token

    base_url = "https://audit-tool.example"
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", base_url)
    clear_settings_cache()

    app, harness = build_paired_http_app(get_settings())
    session = asyncio.run(
        harness.control.session_coordinator.start_session(workdir=str(tmp_path))
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
        params={"session_id": session_id},
        headers=headers("shell:read"),
    )
    listing = client.get(
        "/tools/audit_tail",
        params={"session_id": session_id, "event": "route-large-entry"},
        headers=headers(SCOPE_AUDIT_READ),
    )
    entry_id = listing.json()["entries"][0]["id"]
    denied_full = client.get(
        "/tools/audit_tail",
        params={
            "session_id": session_id,
            "entry_id": entry_id,
            "include_full_payloads": "true",
        },
        headers=headers(SCOPE_AUDIT_READ),
    )
    full = client.get(
        "/tools/audit_tail",
        params={
            "session_id": session_id,
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
