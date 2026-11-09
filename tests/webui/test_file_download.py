"""Real HTTP ticket behavior with the Executor's local Files policy."""

import os

import pytest
from fastapi.testclient import TestClient

from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.executor.files import FilesConfig
from workgate.executor.tool_session.store import ToolSessionStore
from workgate.executor.ui_files import UiFilesService
from workgate.persistence import FileStateStore
from workgate.tools.catalog import ToolCatalog
from workgate.ui.http.file_download import BrowserDownloadTickets, _range_bounds


@pytest.fixture
def download_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    store = ToolSessionStore(
        FileStateStore(lambda: tmp_path / "state"),
        default_workdir=tmp_path,
        max_session_snapshots=20,
        max_session_snapshot_bytes=2_000_000,
    )
    service = UiFilesService(
        FilesConfig(
            default_workdir=tmp_path,
            max_directory_entries=50,
            max_file_read_bytes=64,
            max_file_write_bytes=64,
        ),
        store,
    )
    app = build_http_app(tool_catalog=ToolCatalog(()))
    app.state.control_runtime = object()
    from workgate.ui.http import file_download

    async def executor_call(runtime, executor_id, op, args):
        assert runtime is app.state.control_runtime
        assert executor_id == "test-executor"
        return executor_id, await service.execute(op, dict(args))

    monkeypatch.setattr(file_download, "call_ui_executor", executor_call)
    client = TestClient(app)
    try:
        yield client, service, tmp_path
    finally:
        client.close()
        clear_settings_cache()


def _issue(client, path):
    return client.post(
        "/api/ui/files/download-ticket",
        json={"executor_id": "test-executor", "path": path},
    )


def test_browser_download_complete_and_single_use(download_setup):
    client, _, root = download_setup
    data = bytes(range(256)) * 4098 + b"final"
    (root / "report.bin").write_bytes(data)
    response = _issue(client, "report.bin")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    url = response.json()["data"]["url"]
    assert url.startswith("/api/ui/files/download/")
    download = client.get(url)
    assert download.status_code == 200
    assert download.content == data
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-length"] == str(len(data))
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["referrer-policy"] == "no-referrer"
    assert "attachment" in download.headers["content-disposition"]
    assert client.get(url).status_code == 404
    assert client.get("/api/ui/files/download/no-ticket").status_code == 404


@pytest.mark.parametrize(
    ("range_header", "expected", "content_range"),
    [
        ("bytes=3-8", b"345678", "bytes 3-8/16"),
        ("bytes=14-", b"ef", "bytes 14-15/16"),
        ("bytes=-4", b"cdef", "bytes 12-15/16"),
        ("bytes=0-999", b"0123456789abcdef", "bytes 0-15/16"),
    ],
)
def test_browser_download_ranges(
    download_setup, range_header, expected, content_range
):
    client, _, root = download_setup
    (root / "range.bin").write_bytes(b"0123456789abcdef")
    url = _issue(client, "range.bin").json()["data"]["url"]
    response = client.get(url, headers={"Range": range_header})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-range"] == content_range
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize(
    "range_header",
    ["bytes=100-", "bytes=6-2", "bytes=-0", "bytes=0-1,5-6", "units=0-1"],
)
def test_browser_download_rejects_invalid_ranges(download_setup, range_header):
    client, _, root = download_setup
    (root / "range.bin").write_bytes(b"12345")
    url = _issue(client, "range.bin").json()["data"]["url"]
    response = client.get(url, headers={"Range": range_header})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */5"
    assert client.get(url).status_code == 404


def test_browser_download_empty_and_utf8_name(download_setup):
    client, _, root = download_setup
    (root / "测试 资料.bin").write_bytes(b"")
    url = _issue(client, "测试 资料.bin").json()["data"]["url"]
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == b""
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_browser_download_stale_file_fails_before_stream(download_setup):
    client, _, root = download_setup
    file = root / "change.bin"
    file.write_bytes(b"Original")
    url = _issue(client, "change.bin").json()["data"]["url"]
    file.write_bytes(b"Modified larger content")
    response = client.get(url)
    assert response.status_code == 409
    assert client.get(url).status_code == 404


def test_executor_chunk_identity_and_bounds(download_setup):
    _, service, root = download_setup
    file = root / "chunk.bin"
    file.write_bytes(b"Hello World")
    info = service.download_info("chunk.bin")
    assert (
        service.download_chunk("chunk.bin", info["identity"], 0, 5)["bytes"]
        == 5
    )
    with pytest.raises(ValueError, match="exceeds"):
        service.download_chunk("chunk.bin", info["identity"], 0, 256 * 1024 + 1)
    with pytest.raises(ValueError, match="changed"):
        service.download_chunk("chunk.bin", info["identity"], 11, 1)
    file.write_bytes(b"Replaced content")
    with pytest.raises(ValueError, match="changed"):
        service.download_chunk("chunk.bin", info["identity"], 0, 1)
    if os.name != "nt":
        (root / "symlink.bin").symlink_to(file)
        info = service.download_info("symlink.bin")
        assert (
            service.download_chunk("symlink.bin", info["identity"], 0, 1)[
                "bytes"
            ]
            == 1
        )


def test_ticket_capacity_expiry_and_replay():
    import time

    store = BrowserDownloadTickets()
    info = {
        "path": "a.bin",
        "name": "a.bin",
        "size": 0,
        "identity": [1, 2, 0, 4, 5],
    }
    token = store.issue(executor_id="ex", info=info)
    assert store.consume(token) is not None
    assert store.consume(token) is None
    for _ in range(32):
        store.issue(executor_id="ex", info=info)
    with pytest.raises(ValueError, match="pending"):
        store.issue(executor_id="ex", info=info)
    token2 = next(iter(store._tickets))
    previous = store._tickets[token2]
    from dataclasses import replace

    store._tickets[token2] = replace(previous, expires_at=time.monotonic() - 1)
    assert store.consume(token2) is None


@pytest.mark.parametrize(
    "header,size", [(None, 0), ("bytes=-2", 3), ("bytes=0-1", 2)]
)
def test_single_range_bounds(header, size):
    start, end, partial = _range_bounds(header, size)
    assert 0 <= start <= end <= size
    assert partial is (header is not None)


def test_download_ticket_requires_oauth_and_read_scope(monkeypatch, tmp_path):
    from workgate.oauth.core.state import OAuthState, use_oauth_state
    from workgate.oauth.protocol.token_codec import issue_access_token
    from workgate.ui.http import file_download

    base_url = "https://control.example.test"
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", base_url)
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    store = ToolSessionStore(
        FileStateStore(lambda: tmp_path / "state"),
        default_workdir=tmp_path,
        max_session_snapshots=20,
        max_session_snapshot_bytes=2_000_000,
    )
    service = UiFilesService(FilesConfig(tmp_path, 50, 64, 64), store)
    (tmp_path / "authorized.bin").write_bytes(b"authorized")
    with use_oauth_state(OAuthState(tmp_path / "oauth")):
        app = build_http_app(tool_catalog=ToolCatalog(()))
        app.state.control_runtime = object()

        async def fake_call(_runtime, executor_id, op, args):
            assert executor_id == "test-executor"
            return executor_id, await service.execute(op, args)

        monkeypatch.setattr(file_download, "call_ui_executor", fake_call)
        with TestClient(
            app, base_url=base_url, client=("203.0.113.10", 50000)
        ) as client:
            assert _issue(client, "authorized.bin").status_code == 401
            unauthorized = issue_access_token(
                client_id="test-reader",
                scope="shell:execute",
                resource=f"{base_url}/mcp",
            )
            payload = {"executor_id": "test-executor", "path": "authorized.bin"}
            denied = client.post(
                "/api/ui/files/download-ticket",
                json=payload,
                headers={"Authorization": f"Bearer {unauthorized}"},
            )
            assert denied.status_code == 403
            authorized = issue_access_token(
                client_id="test-reader",
                scope="shell:read",
                resource=f"{base_url}/mcp",
            )
            issued = client.post(
                "/api/ui/files/download-ticket",
                json=payload,
                headers={"Authorization": f"Bearer {authorized}"},
            )
            assert issued.status_code == 200
            url = issued.json()["data"]["url"]
            # The one-use ticket deliberately allows an attachment GET without
            # exposing OAuth credentials or an Executor bearer token.
            assert client.get(url).content == b"authorized"
            assert client.get(url).status_code == 404
    clear_settings_cache()
