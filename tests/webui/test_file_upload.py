"""Browser upload RPC bounds, OAuth ownership, and transactional failure paths."""

import base64
import hashlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.core.state import OAuthState, use_oauth_state
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.tools.catalog import ToolCatalog
from workgate.ui.http import file_upload


@pytest.fixture
def upload_api(
    monkeypatch, tmp_path
) -> Iterator[tuple[TestClient, dict, dict]]:
    base_url = "https://upload.example.test"
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", base_url)
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".control"))
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    state = {"files": {}, "transfers": {}, "aborted": []}
    with use_oauth_state(OAuthState(tmp_path / "oauth")):
        app = build_http_app(tool_catalog=ToolCatalog(()))
        app.state.control_runtime = object()

        async def fake_rpc(runtime, executor_id, op, args):
            assert runtime is app.state.control_runtime
            assert executor_id == "ex1"
            if op == "transfer_begin_write":
                assert args["overwrite"] is False
                path = str(args["path"])
                if path in state["files"]:
                    raise FileExistsError(path)
                transfer_id = f"transfer-{len(state['transfers']) + 1}"
                state["transfers"][transfer_id] = {
                    "path": path,
                    "size": int(args["expected_bytes"]),
                    "data": bytearray(),
                }
                return executor_id, {"path": path, "transfer_id": transfer_id}
            transfer_id = str(args["transfer_id"])
            transfer = state["transfers"][transfer_id]
            assert args["path"] == transfer["path"]
            if op == "transfer_write_chunk":
                data = base64.b64decode(args["data_b64"], validate=True)
                offset = int(args["offset"])
                assert offset == len(transfer["data"])
                digest = hashlib.sha256(data).hexdigest()
                assert args["expected_sha256"] == digest
                transfer["data"].extend(data)
                return executor_id, {
                    "offset": offset,
                    "bytes": len(data),
                    "sha256": digest,
                }
            if op == "transfer_finish_write":
                data = bytes(transfer["data"])
                digest = hashlib.sha256(data).hexdigest()
                assert len(data) == transfer["size"] == args["expected_bytes"]
                assert digest == args["expected_sha256"]
                state["files"][transfer["path"]] = data
                return executor_id, {
                    "completed": True,
                    "bytes": len(data),
                    "sha256": digest,
                }
            if op == "transfer_abort_write":
                state["aborted"].append(transfer_id)
                state["transfers"].pop(transfer_id)
                return executor_id, {"aborted": True}
            raise AssertionError(op)

        monkeypatch.setattr(file_upload, "call_ui_executor", fake_rpc)
        access = {}
        for who, scope in (
            ("owner", "shell:read shell:write"),
            ("visitor", "shell:read shell:write"),
            ("read-only", "shell:read"),
        ):
            token = issue_access_token(
                client_id="webui-upload-test",
                subject=who,
                scope=scope,
                resource=f"{base_url}/mcp",
            )
            access[who] = {"Authorization": f"Bearer {token}"}
        with TestClient(
            app, base_url=base_url, client=("203.0.113.10", 50000)
        ) as client:
            yield client, state, access
    clear_settings_cache()


def start(client, headers, *, name="example.bin", size=7):
    return client.post(
        "/api/ui/files/upload/start",
        json={"executor_id": "ex1", "path": name, "size": size},
        headers=headers,
    )


def chunk(client, headers, token, offset, data):
    return client.put(
        f"/api/ui/files/upload/{token}/chunk",
        content=data,
        headers={**headers, "X-Workgate-Upload-Offset": str(offset)},
    )


def test_stream_upload_complete_one_use_and_no_overwrite(upload_api):
    client, state, access = upload_api
    original = bytes(range(256)) * 1125 + b"1234567"
    started = start(client, access["owner"], size=len(original))
    assert started.status_code == 200
    assert started.headers["cache-control"] == "no-store"
    token = started.json()["data"]["token"]
    assert started.json()["data"]["chunk_bytes"] == 262144
    for offset in range(0, len(original), 262144):
        piece = original[offset : offset + 262144]
        reply = chunk(client, access["owner"], token, offset, piece)
        assert reply.status_code == 200, reply.text
        assert reply.json()["data"]["offset"] == offset + len(piece)
    finished = client.post(
        f"/api/ui/files/upload/{token}/finish", headers=access["owner"]
    )
    assert finished.status_code == 200, finished.text
    assert state["files"]["example.bin"] == original
    assert (
        client.post(
            f"/api/ui/files/upload/{token}/finish", headers=access["owner"]
        ).status_code
        == 404
    )
    assert start(client, access["owner"], size=1).status_code == 400
    assert state["files"]["example.bin"] == original


def test_upload_owner_scope_and_offset_replay(upload_api):
    client, state, access = upload_api
    assert start(client, access["read-only"]).status_code == 403
    assert start(client, {}).status_code == 401
    started = start(client, access["owner"])
    token = started.json()["data"]["token"]
    assert chunk(client, access["visitor"], token, 0, b"hi").status_code == 404
    assert (
        client.post(
            f"/api/ui/files/upload/{token}/finish", headers=access["visitor"]
        ).status_code
        == 404
    )
    assert chunk(client, access["owner"], token, 0, b"hi").status_code == 200
    assert chunk(client, access["owner"], token, 0, b"hi").status_code == 400
    assert chunk(client, access["owner"], token, 3, b"hi").status_code == 400
    assert (
        client.post(
            f"/api/ui/files/upload/{token}/finish", headers=access["owner"]
        ).status_code
        == 400
    )
    assert "example.bin" not in state["files"]
    abort = client.post(
        f"/api/ui/files/upload/{token}/abort", headers=access["owner"]
    )
    assert abort.status_code == 200
    assert state["aborted"] == ["transfer-1"]
    assert (
        client.post(
            f"/api/ui/files/upload/{token}/abort", headers=access["owner"]
        ).status_code
        == 404
    )


def test_upload_chunk_size_and_expiry(upload_api):
    client, state, access = upload_api
    token = start(client, access["owner"], size=262145).json()["data"]["token"]
    assert (
        chunk(client, access["owner"], token, 0, b"a" * 262145).status_code
        == 413
    )
    assert (
        chunk(client, access["owner"], token, 0, b"a" * 262144).status_code
        == 200
    )
    record = client.app.state.browser_uploads.get(token)
    assert record is not None
    record.expires_at = 0
    assert (
        chunk(client, access["owner"], token, 262144, b"a").status_code == 404
    )
    assert "example.bin" not in state["files"]


def test_upload_empty_and_cancelled(upload_api):
    client, state, access = upload_api
    token = start(client, access["owner"], name="empty.bin", size=0).json()[
        "data"
    ]["token"]
    assert (
        client.post(
            f"/api/ui/files/upload/{token}/finish", headers=access["owner"]
        ).status_code
        == 200
    )
    assert state["files"]["empty.bin"] == b""
    token = start(client, access["owner"], name="stop.bin", size=12).json()[
        "data"
    ]["token"]
    assert chunk(client, access["owner"], token, 0, b"half").status_code == 200
    assert (
        client.post(
            f"/api/ui/files/upload/{token}/abort", headers=access["owner"]
        ).status_code
        == 200
    )
    assert "stop.bin" not in state["files"]


@pytest.mark.parametrize("size", [-1, True, "1", 8 * 1024**3 + 1])
def test_upload_invalid_size(upload_api, size):
    client, _, access = upload_api
    assert start(client, access["owner"], size=size).status_code == 400
