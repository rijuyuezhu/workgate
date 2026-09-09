from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import build_paired_http_app
from workgate.audit import audit, audit_tool_call_end, audit_tool_call_start
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.oauth.core.scopes import (
    SCOPE_AUDIT_FULL,
    SCOPE_AUDIT_READ,
    SCOPE_SHELL_WRITE,
)
from workgate.oauth.protocol.token_codec import issue_access_token

BASE_URL = "https://workgate.example"


def _configure(monkeypatch, tmp_path, *, auth_mode: str = "none") -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    clear_settings_cache()


async def _client_with_session(
    monkeypatch, tmp_path, *, auth_mode: str = "none"
):
    _configure(monkeypatch, tmp_path, auth_mode=auth_mode)
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    app, harness = build_paired_http_app(get_settings())
    started = await harness.control.session_coordinator.start_session(
        workdir="project", executor_id=harness.executor_id
    )
    assert isinstance(started, dict)
    client = TestClient(app, base_url=BASE_URL, client=("203.0.113.14", 50005))
    return client, harness, str(started["session_id"])


def _token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-audit-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def _headers(scope: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(scope)}"}


@pytest.mark.asyncio
async def test_audit_lists_filters_and_returns_metadata_only_summaries(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    audit(
        "canonical-one",
        session_id=session_id,
        operation="files",
        payload={"token": "secret-value", "body": "visible"},
    )
    audit("canonical-two", operation="shell", command="echo hi")

    response = client.get(
        "/api/ui/audit",
        params={"event": "canonical-one", "sort": "asc", "limit": 10},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["scope"] == "global"
    assert data["count"] == 1
    assert data["total_matched"] == 1
    entry = data["entries"][0]
    assert entry["event"] == "canonical-one"
    assert entry["session"] == session_id
    assert entry["node"] == "control"
    assert "payload" not in entry
    assert data["limits"]["entries"] == 2000


@pytest.mark.asyncio
async def test_session_scoped_audit_uses_canonical_control_log(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    (tmp_path / "other").mkdir()
    other = await harness.control.session_coordinator.start_session(
        workdir="other", executor_id=harness.executor_id
    )
    assert isinstance(other, dict)
    other_id = str(other["session_id"])
    audit("owned-one", session_id=session_id, operation="files")
    audit("owned-other", session_id=other_id, operation="files")

    response = client.get(
        "/api/ui/audit",
        params={"scope": "session", "session": session_id, "limit": 50},
    )
    missing_session = client.get("/api/ui/audit", params={"scope": "session"})
    unknown = client.get(
        "/api/ui/audit",
        params={"scope": "session", "session": "sess_0000000000000000000000"},
    )

    assert response.status_code == 200
    entries = response.json()["data"]["entries"]
    assert {entry["event"] for entry in entries} == {"owned-one"}
    assert all(entry["session"] == session_id for entry in entries)
    assert missing_session.status_code == 400
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_audit_detail_enforces_operation_sensitive_scopes(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path, auth_mode="oauth"
    )
    audit_tool_call_start(
        call_id="write-detail",
        transport="http",
        tool="write_file",
        input={
            "session_id": session_id,
            "path": "note.txt",
            "content": "secret",
        },
    )
    audit_tool_call_end(
        call_id="write-detail",
        transport="http",
        tool="write_file",
        ok=True,
        duration_ms=1,
        output={"path": "note.txt"},
    )

    read_only = _headers(SCOPE_AUDIT_READ)
    allowed = _headers(f"{SCOPE_AUDIT_READ} {SCOPE_SHELL_WRITE}")
    denied = client.get(
        "/api/ui/audit/detail",
        params={"id": "call:write-detail"},
        headers=read_only,
    )
    detail = client.get(
        "/api/ui/audit/detail",
        params={"id": "call:write-detail"},
        headers=allowed,
    )

    assert denied.status_code == 403
    assert SCOPE_SHELL_WRITE in denied.text
    assert detail.status_code == 200
    entry = detail.json()["data"]["entry"]
    assert entry["tool"] == "write_file"
    assert entry["session"] == session_id


@pytest.mark.asyncio
async def test_audit_detail_full_payloads_require_audit_full(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path, auth_mode="oauth"
    )
    audit_tool_call_start(
        call_id="read-full",
        transport="http",
        tool="read",
        input={"session_id": session_id, "path": "note.txt"},
    )
    audit_tool_call_end(
        call_id="read-full",
        transport="http",
        tool="read",
        ok=True,
        duration_ms=1,
        output={"content": "retained-body"},
    )

    read_only = _headers(SCOPE_AUDIT_READ)
    full_scope = _headers(f"{SCOPE_AUDIT_READ} {SCOPE_AUDIT_FULL}")
    denied = client.get(
        "/api/ui/audit/detail",
        params={"id": "call:read-full", "include_full_payloads": "true"},
        headers=read_only,
    )
    full = client.get(
        "/api/ui/audit/detail",
        params={"id": "call:read-full", "include_full_payloads": "true"},
        headers=full_scope,
    )

    assert denied.status_code == 403
    assert SCOPE_AUDIT_FULL in denied.text
    assert full.status_code == 200
    entry = full.json()["data"]["entry"]
    assert entry["input"]["path"] == "note.txt"
    assert entry["output"]["content"] == "retained-body"


@pytest.mark.asyncio
async def test_audit_selected_preview_degrades_when_detail_scope_is_missing(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path, auth_mode="oauth"
    )
    audit_tool_call_start(
        call_id="selected-write",
        transport="http",
        tool="write_file",
        input={
            "session_id": session_id,
            "path": "note.txt",
            "content": "value",
        },
    )
    audit_tool_call_end(
        call_id="selected-write",
        transport="http",
        tool="write_file",
        ok=True,
        duration_ms=1,
        output={"path": "note.txt"},
    )

    response = client.get(
        "/api/ui/audit",
        params={
            "event": "tool_call_end",
            "include_selected": "true",
            "selected_id": "call:selected-write",
        },
        headers=_headers(SCOPE_AUDIT_READ),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert "entry" not in data
    assert SCOPE_SHELL_WRITE in data["entry_error"]
    assert any(row["id"] == "call:selected-write" for row in data["entries"])


@pytest.mark.asyncio
async def test_audit_api_validates_bounds_and_unknown_details(
    tmp_path, monkeypatch
):
    client, _harness, _session_id = await _client_with_session(
        monkeypatch, tmp_path
    )

    assert client.get("/api/ui/audit", params={"limit": 0}).status_code == 400
    assert (
        client.get("/api/ui/audit", params={"sort": "sideways"}).status_code
        == 400
    )
    assert (
        client.get(
            "/api/ui/audit", params={"start_ts": 2, "end_ts": 1}
        ).status_code
        == 400
    )
    missing = client.get("/api/ui/audit/detail", params={"id": "missing"})
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_audit_detail_sanitizes_and_previews_view_image(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
    )
    audit_tool_call_start(
        call_id="image-detail",
        tool="view_image",
        transport="http",
        input={"session_id": session_id, "path": "pixel.png"},
    )
    audit_tool_call_end(
        call_id="image-detail",
        tool="view_image",
        transport="http",
        ok=True,
        duration_ms=1,
        output={
            "content": [
                {
                    "type": "image",
                    "data": base64.b64encode(png).decode("ascii"),
                    "mimeType": "image/png",
                }
            ],
            "structuredContent": {"path": "pixel.png"},
        },
    )

    response = client.get(
        "/api/ui/audit/detail",
        params={
            "id": "call:image-detail",
            "columns": 10,
            "rows": 5,
            "cell_aspect": 2,
        },
    )

    assert response.status_code == 200
    entry = response.json()["data"]["entry"]
    assert "data" not in entry["output"]["content"][0]
    assert entry["output"]["content"][0]["bytes"] == len(png)
    preview = entry["image_preview"]
    assert preview["path"] == "pixel.png"
    assert preview["bytes"] == len(png)
    assert preview["mime_type"] == "image/png"
    assert (
        len(base64.b64decode(preview["rgba"]))
        == preview["width"] * preview["height"] * 4
    )


@pytest.mark.asyncio
async def test_audit_detail_never_echoes_invalid_inline_image_data(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    audit_tool_call_start(
        call_id="invalid-image",
        tool="view_image",
        transport="http",
        input={"session_id": session_id, "path": "bad.png"},
    )
    audit_tool_call_end(
        call_id="invalid-image",
        tool="view_image",
        transport="http",
        ok=False,
        duration_ms=1,
        output={"content": [{"type": "image", "data": "not-base64"}]},
    )

    response = client.get(
        "/api/ui/audit/detail", params={"id": "call:invalid-image"}
    )

    assert response.status_code == 200
    entry = response.json()["data"]["entry"]
    assert "data" not in entry["output"]["content"][0]
    assert "image_preview" not in entry
    assert entry["image_preview_error"]


def test_audit_static_ui_avoids_html_injection_for_untrusted_details() -> None:
    static_root = (
        Path(__file__).parents[1] / "src" / "workgate" / "ui" / "static"
    )
    audit_script = (static_root / "audit.js").read_text(encoding="utf-8")
    audit_view = (static_root / "audit_view.js").read_text(encoding="utf-8")

    assert "innerHTML" not in audit_view
    assert "elements.auditDetailBody.innerHTML" not in audit_script
    assert "textContent" in audit_view
