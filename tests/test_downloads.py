import base64
import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient as FastAPITestClient
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.helpers import build_paired_http_app
from workgate.config.settings import (
    Settings,
    clear_settings_cache,
    get_settings,
)
from workgate.control.download_snapshot import (
    DownloadSnapshot,
    assert_shareable_size,
    new_staging_path,
    open_private_staging,
    snapshot_directory,
)
from workgate.control.download_store import backup_path
from workgate.control.downloads import (
    _list_file_links_owned,
    _register_snapshot,
    _revoke_file_link_owned,
    download_token_fingerprint,
)
from workgate.control.mcp.app import build_mcp
from workgate.http.downloads import _token_fingerprint, download_routes


def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_DATA_DIR", str(tmp_path / ".data"))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://files.example.test")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()


def _register_file(
    source: Path,
    *,
    ttl_s: int = 60,
    filename: str | None = None,
    max_downloads: int | None = None,
    inline: bool = False,
    session_id: str | None = None,
    settings: Settings | None = None,
):
    data = source.read_bytes()
    data_dir = None if settings is None else settings.data_dir
    staging = new_staging_path(data_dir=data_dir)
    with open_private_staging(staging, data_dir=data_dir) as handle:
        handle.write(data)
    return _register_snapshot(
        DownloadSnapshot(
            staging_path=staging,
            display_path=source.name,
            source_name=source.name,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        ),
        ttl_s=ttl_s,
        filename=filename,
        max_downloads=max_downloads,
        inline=inline,
        session_id=session_id,
        settings=settings,
    )


def _public_session(client: FastAPITestClient) -> str:
    response = client.post("/tools/session_start", json={"workdir": "."})
    assert response.status_code == 200
    return str(response.json()["session_id"])


def test_shareable_size_rejects_invalid_and_over_limit_values() -> None:
    with pytest.raises(ValueError, match="File size is invalid"):
        assert_shareable_size(-1, maximum=10)
    with pytest.raises(ValueError, match="File is too large: 11"):
        assert_shareable_size(11, maximum=10)


def test_public_create_share_link_serves_executor_snapshot(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    app, _harness = build_paired_http_app(get_settings())
    client = FastAPITestClient(app)
    session_id = _public_session(client)

    created = client.post(
        "/tools/file_link/create",
        json={
            "session_id": session_id,
            "path": "hello.txt",
            "ttl_s": 60,
            "filename": "result.txt",
            "max_downloads": 2,
        },
    )
    assert created.status_code == 200
    link = created.json()
    assert link["url"].startswith("https://files.example.test/download/")
    assert link["link_id"].startswith("link_")
    assert "payload_id" not in link
    listed = client.get(
        "/tools/file_link/list", params={"session_id": session_id}
    ).json()["links"]
    assert listed[0]["link_id"] == link["link_id"]
    assert listed[0]["token_fingerprint"] == download_token_fingerprint(
        link["token"]
    )
    assert "token" not in listed[0]
    assert "url" not in listed[0]

    response = client.get(link["url"])
    assert response.status_code == 200
    assert response.text == "hello"
    assert "result.txt" in response.headers["content-disposition"]


def test_explicit_control_data_dir_overrides_ambient_payload_location(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "hello.txt"
    source.write_text("hello", encoding="utf-8")
    ambient = get_settings()
    explicit = ambient.model_copy(
        update={"data_dir": tmp_path / "explicit-control-data"}
    )

    created = _register_file(source, settings=explicit)

    explicit_payloads = explicit.data_dir / "control" / "payloads" / "download"
    ambient_payloads = ambient.data_dir / "control" / "payloads" / "download"
    assert len(list(explicit_payloads.glob("*.bin"))) == 1
    assert not ambient_payloads.exists()

    client = TestClient(Starlette(routes=download_routes(explicit)))
    response = client.get(created.url)
    assert response.status_code == 200
    assert response.content == b"hello"


def test_share_link_expiry_revocation_and_download_limit(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "hello.txt"
    source.write_text("hello", encoding="utf-8")
    client = TestClient(Starlette(routes=download_routes()))

    revoked = _register_file(source, ttl_s=60)
    assert _revoke_file_link_owned(revoked.link_id).revoked is True
    assert client.get(revoked.url).status_code == 404

    expired = _register_file(source, ttl_s=1)
    time.sleep(1.05)
    assert client.get(expired.url).status_code == 410

    once = _register_file(source, max_downloads=1)
    assert client.get(once.url).status_code == 200
    assert client.get(once.url).status_code == 410


def test_file_links_are_shared_session_owned(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    app, _harness = build_paired_http_app(get_settings())
    client = FastAPITestClient(app)
    first = _public_session(client)
    second = _public_session(client)

    link = client.post(
        "/tools/file_link/create",
        json={"session_id": first, "path": "hello.txt"},
    ).json()

    first_links = client.get(
        "/tools/file_link/list", params={"session_id": first}
    ).json()["links"]
    second_links = client.get(
        "/tools/file_link/list", params={"session_id": second}
    ).json()["links"]
    assert [item["link_id"] for item in first_links] == [link["link_id"]]
    assert "token" not in first_links[0]
    assert "url" not in first_links[0]
    assert second_links == []

    wrong_owner = client.post(
        "/tools/file_link/revoke",
        json={"session_id": second, "link_id": link["link_id"]},
    )
    assert wrong_owner.status_code == 200
    assert wrong_owner.json()["revoked"] is False
    owner = client.post(
        "/tools/file_link/revoke",
        json={"session_id": first, "link_id": link["link_id"]},
    )
    assert owner.status_code == 200
    assert owner.json()["revoked"] is True


@pytest.mark.asyncio
async def test_file_link_tools_are_registered(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    clear_settings_cache()
    tools = {tool.name: tool for tool in await build_mcp().list_tools()}

    assert {"create_file_link", "list_file_links", "revoke_file_link"} <= set(
        tools
    )
    create_tool = tools["create_file_link"]
    list_tool = tools["list_file_links"]
    assert create_tool.outputSchema is not None
    assert list_tool.outputSchema is not None
    assert create_tool.outputSchema["title"] == "CreateFileLinkOutput"
    assert list_tool.outputSchema["title"] == "ListFileLinksOutput"
    description = create_tool.inputSchema["properties"]["path"]["description"]
    assert "file" in description.lower()
    assert "download" in description.lower()
    assert create_tool.inputSchema["properties"]["inline"]["default"] is False
    assert "url" in create_tool.outputSchema["properties"]
    assert "target" not in create_tool.outputSchema["properties"]
    revoke_tool = tools["revoke_file_link"]
    assert "link_id" in revoke_tool.inputSchema["properties"]
    assert "token" not in revoke_tool.inputSchema["properties"]


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
    assert fingerprint == download_token_fingerprint(token)


def test_download_store_never_persists_bearer_token_or_url(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "private.txt"
    source.write_text("payload", encoding="utf-8")

    created = _register_file(source)
    persisted = (get_settings().state_dir / "downloads.json").read_text(
        encoding="utf-8"
    )
    parsed = json.loads(persisted)
    record = parsed["links"][created.link_id]

    assert created.token not in persisted
    assert created.url not in persisted
    assert parsed["version"] == 3
    assert record["link_id"] == created.link_id
    assert (
        record["token_sha256"]
        == hashlib.sha256(created.token.encode("utf-8")).hexdigest()
    )
    assert record["token_fingerprint"] == created.token_fingerprint
    assert record["payload_id"].startswith("payload_")
    assert "token" not in record
    assert "url" not in record


def test_download_tokens_are_redacted_from_audit_logs(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    app, _harness = build_paired_http_app(get_settings())
    client = FastAPITestClient(app)
    session_id = _public_session(client)
    link = client.post(
        "/tools/file_link/create",
        json={"session_id": session_id, "path": "hello.txt"},
    ).json()
    token = link["token"]
    assert (
        client.post(
            "/tools/file_link/revoke",
            json={"session_id": session_id, "link_id": link["link_id"]},
        ).status_code
        == 200
    )

    log_text = get_settings().audit_log_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in log_text.splitlines() if line]
    fingerprint = download_token_fingerprint(token)
    assert token not in log_text
    assert link["url"] not in log_text
    assert "/download/<redacted>" in log_text
    assert fingerprint in log_text
    assert any(
        record.get("event") == "download_link_created"
        and record.get("token_fingerprint") == fingerprint
        for record in records
    )
    assert any(
        record.get("event") == "download_link_revoked"
        and record.get("token_fingerprint") == fingerprint
        for record in records
    )


def test_file_link_remains_available_after_executor_goes_offline(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "offline.txt"
    source.write_text("snapshot bytes", encoding="utf-8")
    app, harness = build_paired_http_app(get_settings())
    client = FastAPITestClient(app)
    session_id = _public_session(client)
    response = client.post(
        "/tools/file_link/create",
        json={"session_id": session_id, "path": "offline.txt"},
    )
    assert response.status_code == 200
    link = response.json()

    async def executor_offline(*args, **kwargs):
        raise RuntimeError("executor is offline")

    monkeypatch.setattr(
        harness.control.executor_transport, "call", executor_offline
    )
    source.unlink()
    download = client.get(link["url"])
    assert download.status_code == 200
    assert download.content == b"snapshot bytes"


def test_creation_time_snapshot_and_inline_headers(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "artifact.txt"
    source.write_text("original", encoding="utf-8")
    attachment = _register_file(source)
    source.write_text("changed after link creation", encoding="utf-8")

    client = TestClient(Starlette(routes=download_routes()))
    response = client.get(attachment.url)
    assert response.status_code == 200
    assert response.text == "original"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in response.headers
    assert attachment.media_type == "text/plain"

    payload = b"\x89PNG\r\n\x1a\nmock-png"
    rendered = tmp_path / "rendered"
    rendered.write_bytes(payload)
    inline = _register_file(rendered, filename="plot.png", inline=True)
    head = client.head(inline.url)
    shown = client.get(inline.url)
    assert head.status_code == 200
    assert shown.content == payload
    assert shown.headers["content-type"] == "image/png"
    assert shown.headers["content-disposition"].startswith("inline;")
    assert shown.headers["content-security-policy"] == "sandbox"
    assert shown.headers["referrer-policy"] == "no-referrer"
    assert _list_file_links_owned().links[0].downloads == 1


def test_source_extension_precedes_display_filename_for_mime(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "notes.txt"
    source.write_text("text", encoding="utf-8")
    link = _register_file(source, filename="pretend.png", inline=True)
    assert link.media_type == "text/plain"


def test_final_download_deletes_snapshot_and_tampering_is_rejected(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "once.txt"
    source.write_text("once", encoding="utf-8")
    once = _register_file(source, max_downloads=1)
    client = TestClient(Starlette(routes=download_routes()))
    assert len(list(snapshot_directory().glob("*.bin"))) == 1
    assert client.get(once.url).text == "once"
    assert list(snapshot_directory().glob("*.bin")) == []
    assert client.get(once.url).status_code == 410

    stable = tmp_path / "stable.txt"
    stable.write_text("stable", encoding="utf-8")
    link = _register_file(stable)
    snapshot = next(snapshot_directory().glob("*.bin"))
    snapshot.write_bytes(b"stolen")
    rejected = client.get(link.url)
    assert rejected.status_code == 404
    assert rejected.json()["error"] == "download_missing"
    assert _list_file_links_owned().links == []


def test_download_store_recovers_from_backup(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "hello.txt"
    source.write_text("hello", encoding="utf-8")
    link = _register_file(source)
    download_store_path = get_settings().state_dir / "downloads.json"
    download_store_path.write_text("{broken", encoding="utf-8")

    recovered = _list_file_links_owned()
    assert [item.link_id for item in recovered.links] == [link.link_id]
    assert json.loads(download_store_path.read_text())["version"] == 3
    assert backup_path().exists()
    if os.name != "nt":
        assert download_store_path.stat().st_mode & 0o777 == 0o600
        assert backup_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_invalid_executor_chunk_removes_staging_snapshot(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    payload = b"executor bytes"
    digest = hashlib.sha256(payload).hexdigest()
    _app, harness = build_paired_http_app(get_settings())
    session = await harness.control.session_coordinator.start_session(
        workdir="."
    )
    assert isinstance(session, dict)
    session_id = str(session["session_id"])
    record = harness.control.control_state.snapshot_sessions()[session_id]

    async def fake_call(_record, op: str, args: dict):
        if op == "transfer_stat":
            return {
                "path": "artifact.bin",
                "type": "file",
                "size": len(payload),
                "modified": 0.0,
                "sha256": digest,
            }
        assert op == "transfer_read_chunk"
        return {
            "path": "artifact.bin",
            "offset": int(args["offset"]),
            "bytes": len(payload),
            "size": len(payload),
            "eof": True,
            "sha256": "0" * 64,
            "data_b64": base64.b64encode(payload).decode("ascii"),
        }

    monkeypatch.setattr(harness.control.download_service, "_call", fake_call)
    with pytest.raises(RuntimeError, match="changed while creating snapshot"):
        await harness.control.download_service._export_snapshot(
            record, "artifact.bin"
        )
    assert list(snapshot_directory().iterdir()) == []


def test_download_filename_is_header_safe_and_rfc5987_encoded(
    tmp_path, monkeypatch
):
    _reset(tmp_path, monkeypatch)
    source = tmp_path / "hello.txt"
    source.write_text("hello", encoding="utf-8")
    link = _register_file(source, filename='报告 "final"\\name.txt')
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
