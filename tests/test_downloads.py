import asyncio
import base64
import hashlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient as FastAPITestClient
from starlette.applications import Starlette
from starlette.testclient import TestClient

from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp
from workgate.http.downloads import (
    _token_fingerprint,
    download_routes,
)
from workgate.ops.downloads import (
    create_file_link_dispatch_execute,
    download_token_fingerprint,
    list_file_links_execute,
    revoke_file_link_execute,
)
from workgate.ops.utils.download_snapshot import snapshot_directory
from workgate.ops.utils.download_store import backup_path
from workgate.tool_session.store import get_tool_session_store


def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://files.example.test")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()


def _create_file_link(*args, **kwargs):
    """Exercise the real async dispatcher from synchronous HTTP tests."""
    return asyncio.run(create_file_link_dispatch_execute(*args, **kwargs))


def test_create_share_link_serves_file(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")

    link = _create_file_link(
        "hello.txt", ttl_s=60, filename="result.txt", max_downloads=2
    )

    assert link.url.startswith("https://files.example.test/download/")
    app = Starlette(routes=download_routes())
    response = TestClient(app).get(link.url)

    assert response.status_code == 200
    assert response.text == "hello"
    assert "result.txt" in response.headers["content-disposition"]


def test_share_link_expires_and_can_be_revoked(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")

    link = _create_file_link("hello.txt", ttl_s=1)
    token = link.token
    assert revoke_file_link_execute(token).revoked is True

    app = Starlette(routes=download_routes())
    assert TestClient(app).get(link.url).status_code == 404

    link = _create_file_link("hello.txt", ttl_s=1)
    time.sleep(1.05)
    assert TestClient(app).get(link.url).status_code == 410


def test_share_link_download_limit(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    link = _create_file_link("hello.txt", ttl_s=60, max_downloads=1)
    client = TestClient(Starlette(routes=download_routes()))

    assert client.get(link.url).status_code == 200
    assert client.get(link.url).status_code == 410


def test_share_link_can_be_disabled(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_FILE_DOWNLOAD_ENABLED", "false")
    clear_settings_cache()
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")

    with pytest.raises(PermissionError):
        _create_file_link("hello.txt", ttl_s=60)


def test_file_links_are_session_owned(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    store = get_tool_session_store()
    store.clear()
    first = store.create_session(workdir=".").session_id
    second = store.create_session(workdir=".").session_id

    link = _create_file_link("hello.txt", ttl_s=60, session_id=first)

    assert [
        item.token for item in list_file_links_execute(session_id=first).links
    ] == [link.token]
    assert list_file_links_execute(session_id=second).links == []
    assert (
        revoke_file_link_execute(link.token, session_id=second).revoked is False
    )
    assert (
        revoke_file_link_execute(link.token, session_id=first).revoked is True
    )


@pytest.mark.asyncio
async def test_file_link_tools_are_registered(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    clear_settings_cache()
    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    names = set(tools)

    assert {"create_file_link", "list_file_links", "revoke_file_link"} <= names

    create_tool = tools["create_file_link"]
    list_tool = tools["list_file_links"]
    assert create_tool.outputSchema is not None
    assert list_tool.outputSchema is not None
    assert create_tool.outputSchema["title"] == "CreateFileLinkOutput"
    assert list_tool.outputSchema["title"] == "ListFileLinksOutput"
    path_description = create_tool.inputSchema["properties"]["path"][
        "description"
    ]
    assert "file" in path_description.lower()
    assert "download" in path_description.lower()
    assert create_tool.inputSchema["properties"]["inline"]["default"] is False
    assert "target" in create_tool.outputSchema["properties"]
    assert "url" in create_tool.outputSchema["properties"]


@pytest.mark.asyncio
async def test_file_link_tools_are_hidden_in_stdio(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_MODE", "stdio")
    clear_settings_cache()
    names = {tool.name for tool in await build_mcp().list_tools()}

    assert {
        "create_file_link",
        "list_file_links",
        "revoke_file_link",
    }.isdisjoint(names)


def test_download_token_fingerprint_does_not_expose_token():
    token = "secret-download-token"

    fingerprint = _token_fingerprint(token)

    assert token not in fingerprint
    assert len(fingerprint) == 16
    assert fingerprint == _token_fingerprint(token)


def test_download_tokens_are_redacted_from_audit_logs(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")

    client = FastAPITestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    link = client.post(
        "/tools/file_link/create",
        json={"session_id": session["session_id"], "path": "hello.txt"},
    ).json()
    token = link["token"]
    assert token
    assert (
        client.post(
            "/tools/file_link/revoke",
            json={"session_id": session["session_id"], "token": token},
        ).status_code
        == 200
    )

    log_text = get_settings().audit_log_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in log_text.splitlines() if line]

    assert token not in log_text
    assert link["url"] not in log_text
    assert "/download/<redacted>" in log_text
    assert download_token_fingerprint(token) in log_text
    assert any(
        record.get("event") == "download_link_created"
        and record.get("token_sha256") == download_token_fingerprint(token)
        for record in records
    )
    assert any(
        record.get("event") == "download_link_revoked"
        and record.get("token_sha256") == download_token_fingerprint(token)
        for record in records
    )


def test_file_link_serves_creation_time_snapshot(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "artifact.txt"
    source.write_text("original", encoding="utf-8")

    link = _create_file_link("artifact.txt", ttl_s=60)
    source.write_text("changed after link creation", encoding="utf-8")

    response = TestClient(Starlette(routes=download_routes())).get(link.url)

    assert response.status_code == 200
    assert response.text == "original"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in response.headers
    assert link.inline is False
    assert link.media_type == "text/plain"
    assert link.target == "local"
    assert link.machine is None


def test_inline_link_uses_sandbox_and_filename_mime_fallback(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    payload = b"\x89PNG\r\n\x1a\nmock-png"
    (tmp_path / "rendered").write_bytes(payload)
    link = _create_file_link(
        "rendered",
        ttl_s=60,
        filename="plot.png",
        inline=True,
    )
    client = TestClient(Starlette(routes=download_routes()))

    head = client.head(link.url)
    response = client.get(link.url)

    assert head.status_code == 200
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert link.inline is True
    assert link.media_type == "image/png"
    assert list_file_links_execute().links[0].downloads == 1


def test_source_extension_takes_precedence_over_display_filename(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")

    link = _create_file_link(
        "notes.txt", ttl_s=60, filename="pretend.png", inline=True
    )

    assert link.media_type == "text/plain"


def test_final_download_deletes_private_snapshot(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "once.txt").write_text("once", encoding="utf-8")
    link = _create_file_link("once.txt", ttl_s=60, max_downloads=1)
    client = TestClient(Starlette(routes=download_routes()))

    assert len(list(snapshot_directory().glob("*.bin"))) == 1
    assert client.get(link.url).text == "once"
    assert list(snapshot_directory().glob("*.bin")) == []
    assert client.get(link.url).status_code == 410


def test_tampered_private_snapshot_is_rejected(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "stable.txt").write_text("stable", encoding="utf-8")
    link = _create_file_link("stable.txt", ttl_s=60)
    snapshot = next(snapshot_directory().glob("*.bin"))
    snapshot.write_bytes(b"stolen")

    response = TestClient(Starlette(routes=download_routes())).get(link.url)

    assert response.status_code == 404
    assert response.json()["error"] == "download_missing"
    assert list_file_links_execute().links == []


@pytest.mark.asyncio
async def test_remote_file_link_streams_validated_snapshot(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    payload = b"remote snapshot bytes"
    digest = hashlib.sha256(payload).hexdigest()
    store = get_tool_session_store()
    store.clear()
    remote = store.create_session(
        target="remote",
        workdir="/remote/project",
        machine="worker-a",
        worker_session_id="WORKER12",
    )

    async def fake_remote_call(session, tool, args):
        assert session.session_id == remote.session_id
        if tool == "transfer_stat":
            return {
                "path": "artifact.bin",
                "type": "file",
                "size": len(payload),
                "modified": 0.0,
                "sha256": digest,
            }
        assert tool == "transfer_read_chunk"
        offset = int(args["offset"])
        limit = int(args["chunk_size"])
        data = payload[offset : offset + limit]
        return {
            "path": "artifact.bin",
            "offset": offset,
            "bytes": len(data),
            "size": len(payload),
            "eof": offset + len(data) >= len(payload),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data_b64": base64.b64encode(data).decode("ascii"),
        }

    monkeypatch.setattr(
        "workgate.ops.utils.download_snapshot.call_remote_session_tool",
        fake_remote_call,
    )
    link = await create_file_link_dispatch_execute(
        "artifact.bin",
        ttl_s=60,
        session_id=remote.session_id,
    )

    response = TestClient(Starlette(routes=download_routes())).get(link.url)

    assert response.status_code == 200
    assert response.content == payload
    assert link.target == "remote"
    assert link.machine == "worker-a"


def test_download_store_recovers_from_backup(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    link = _create_file_link("hello.txt", ttl_s=60)
    download_store_path = get_settings().state_dir / "downloads.json"
    download_store_path.write_text("{broken", encoding="utf-8")

    recovered = list_file_links_execute()

    assert [item.token for item in recovered.links] == [link.token]
    assert json.loads(download_store_path.read_text())["version"] == 2
    assert backup_path().exists()
    if os.name != "nt":
        assert download_store_path.stat().st_mode & 0o777 == 0o600
        assert backup_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_invalid_remote_chunk_removes_staging_snapshot(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    payload = b"remote bytes"
    store = get_tool_session_store()
    store.clear()
    remote = store.create_session(
        target="remote",
        workdir="/remote/project",
        machine="worker-a",
        worker_session_id="WORKER12",
    )

    async def fake_remote_call(_session, tool, args):
        if tool == "transfer_stat":
            return {
                "path": "artifact.bin",
                "type": "file",
                "size": len(payload),
                "modified": 0.0,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        return {
            "path": "artifact.bin",
            "offset": int(args["offset"]),
            "bytes": len(payload),
            "size": len(payload),
            "eof": True,
            "sha256": "0" * 64,
            "data_b64": base64.b64encode(payload).decode("ascii"),
        }

    monkeypatch.setattr(
        "workgate.ops.utils.download_snapshot.call_remote_session_tool",
        fake_remote_call,
    )

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        await create_file_link_dispatch_execute(
            "artifact.bin", ttl_s=60, session_id=remote.session_id
        )

    assert list(snapshot_directory().iterdir()) == []


def test_download_filename_is_header_safe_and_rfc5987_encoded(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    link = _create_file_link(
        "hello.txt",
        ttl_s=60,
        filename='报告 "final"\\name.txt',
    )

    response = TestClient(Starlette(routes=download_routes())).get(link.url)
    disposition = response.headers["content-disposition"]

    assert response.status_code == 200
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert 'filename="final_name.txt"' in disposition
    assert "filename*=UTF-8''" in disposition
    assert "%E6%8A%A5%E5%91%8A" in disposition
    assert "%22final%22_name.txt" in disposition
    assert "%5C" not in disposition
    assert response.headers["content-length"] == "5"
