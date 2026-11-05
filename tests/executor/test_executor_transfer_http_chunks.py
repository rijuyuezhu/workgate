"""Executor-side range retries preserve integrity across proxy and ACK failures."""

import hashlib

import httpx
import pytest

import workgate.executor.transfer_http as transfer_http
from tests.executor.test_executor_transfer_http import _runtime


def _source(tmp_path, *, size=8500):
    workspace, config, store, profile, payloads, gateway, app = _runtime(
        tmp_path
    )
    from workgate.protocol.ids import new_session_id

    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    data = bytes((n * 37) % 251 for n in range(size))
    (workspace / "source.bin").write_bytes(data)
    staging = payloads.new_staging_path("transfer")
    lease = gateway.issue_upload(
        executor_id=str(profile.executor_id),
        transfer_id="copy_" + "c" * 22,
        staging_path=staging,
        expected_bytes=len(data),
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )
    args = {
        "session_id": session_id,
        "path": "source.bin",
        "expected_bytes": len(data),
        "expected_sha256": hashlib.sha256(data).hexdigest(),
        "capability_path": lease.path,
        "capability_token": lease.token,
        "http_chunked": True,
    }
    return workspace, config, store, profile, gateway, app, staging, args, data


class _Intercept(httpx.AsyncBaseTransport):
    def __init__(
        self,
        app,
        *,
        max_request_bytes=None,
        drop_one_ack=False,
        on_success=None,
    ):
        self.delegate = httpx.ASGITransport(app=app)
        self.max_request_bytes = max_request_bytes
        self.drop_one_ack = drop_one_ack
        self.on_success = on_success
        self.sent = []
        self.rejected = []
        self.dropped = 0

    async def handle_async_request(self, request):
        size = int(request.headers.get("content-length", "0"))
        self.sent.append((request.headers.get("content-range"), size))
        if self.max_request_bytes is not None and size > self.max_request_bytes:
            self.rejected.append(size)
            return httpx.Response(413, request=request)
        response = await self.delegate.handle_async_request(request)
        if response.status_code == 200 and self.on_success is not None:
            callback = self.on_success
            self.on_success = None
            callback()
        if (
            self.drop_one_ack
            and response.status_code == 200
            and not self.dropped
        ):
            self.dropped += 1
            raise httpx.ReadError(
                "simulated lost acknowledgement", request=request
            )
        return response


def _patch(monkeypatch, app, transport=None):
    if transport is None:
        transport = _Intercept(app)

    def client(profile):
        return httpx.AsyncClient(
            transport=transport,
            base_url=profile.control_url,
            headers={"Authorization": f"Bearer {profile.credential}"},
            follow_redirects=False,
        )

    monkeypatch.setattr(transfer_http, "_control_client", client)
    return transport


@pytest.mark.asyncio
async def test_multi_request_upload_sends_bounded_ranges(tmp_path, monkeypatch):
    _workspace, config, store, profile, _gateway, app, staging, args, data = (
        _source(tmp_path)
    )
    monkeypatch.setattr(
        transfer_http, "DEFAULT_HTTP_TRANSFER_CHUNK_BYTES", 1024
    )
    transport = _patch(monkeypatch, app)
    result = await transfer_http.upload_to_control(profile, config, store, args)
    assert result == {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "http_chunks": 9,
        "http_chunk_size": 1024,
    }
    assert staging.read_bytes() == data
    assert len(transport.sent) >= 8
    assert all(size <= 1024 for _, size in transport.sent)
    assert transport.sent[0] == (f"bytes 0-1023/{len(data)}", 1024)
    assert transport.sent[-1][0].endswith(f"/{len(data)}")


@pytest.mark.asyncio
async def test_proxy_413_halves_http_request_without_losing_offset(
    tmp_path, monkeypatch
):
    _workspace, config, store, profile, _gateway, app, staging, args, data = (
        _source(tmp_path)
    )
    monkeypatch.setattr(
        transfer_http, "DEFAULT_HTTP_TRANSFER_CHUNK_BYTES", 4096
    )
    transport = _patch(
        monkeypatch, app, _Intercept(app, max_request_bytes=1300)
    )
    result = await transfer_http.upload_to_control(profile, config, store, args)
    assert result["bytes"] == len(data)
    assert staging.read_bytes() == data
    assert transport.rejected == [4096, 2048]
    assert transport.sent[:3] == [
        (f"bytes 0-4095/{len(data)}", 4096),
        (f"bytes 0-2047/{len(data)}", 2048),
        (f"bytes 0-1023/{len(data)}", 1024),
    ]
    assert max(size for _, size in transport.sent[2:]) <= 1024


@pytest.mark.asyncio
async def test_lost_chunk_ack_is_replayed_without_duplicate_bytes(
    tmp_path, monkeypatch
):
    _workspace, config, store, profile, _gateway, app, staging, args, data = (
        _source(tmp_path)
    )
    monkeypatch.setattr(
        transfer_http, "DEFAULT_HTTP_TRANSFER_CHUNK_BYTES", 1024
    )
    transport = _patch(monkeypatch, app, _Intercept(app, drop_one_ack=True))
    result = await transfer_http.upload_to_control(profile, config, store, args)
    assert result["bytes"] == len(data)
    assert transport.dropped == 1
    assert transport.sent[0] == transport.sent[1]
    assert staging.read_bytes() == data


@pytest.mark.asyncio
async def test_changed_source_is_rejected_without_completing_upload(
    tmp_path, monkeypatch
):
    workspace, config, store, profile, _gateway, app, staging, args, data = (
        _source(tmp_path)
    )
    monkeypatch.setattr(
        transfer_http, "DEFAULT_HTTP_TRANSFER_CHUNK_BYTES", 1024
    )
    changed = []

    def mutate():
        (workspace / "source.bin").write_bytes(b"X" * len(data))
        changed.append(True)

    _patch(monkeypatch, app, _Intercept(app, on_success=mutate))
    with pytest.raises(
        (RuntimeError, ValueError), match="integrity|changed|HTTP 409"
    ):
        await transfer_http.upload_to_control(profile, config, store, args)
    assert changed
    # Buffered reads can complete the original bytes before the fstat catches
    # mutation; the owning SessionCopy discards its staging on this error.
    assert staging.stat().st_size <= len(data)


@pytest.mark.asyncio
async def test_invalid_ack_and_retry_exhaustion_fail_closed(
    tmp_path, monkeypatch
):
    _workspace, config, store, profile, _gateway, _app, _staging, args, data = (
        _source(tmp_path)
    )
    monkeypatch.setattr(
        transfer_http, "DEFAULT_HTTP_TRANSFER_CHUNK_BYTES", 1024
    )

    def client_with(response):
        return lambda profile: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: response),
            base_url=profile.control_url,
            headers={"Authorization": f"Bearer {profile.credential}"},
        )

    monkeypatch.setattr(
        transfer_http,
        "_control_client",
        client_with(
            httpx.Response(
                200,
                json={
                    "offset": len(data),
                    "bytes": len(data),
                    "completed": True,
                },
            )
        ),
    )
    with pytest.raises(RuntimeError, match="acknowledgement is invalid"):
        await transfer_http.upload_to_control(profile, config, store, args)

    monkeypatch.setattr(
        transfer_http,
        "_control_client",
        client_with(httpx.Response(503, json={"error": "unavailable"})),
    )
    monkeypatch.setattr(transfer_http.asyncio, "sleep", _no_sleep)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await transfer_http.upload_to_control(profile, config, store, args)


async def _no_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_lost_final_ack_replays_complete_range(tmp_path, monkeypatch):
    _workspace, config, store, profile, _gateway, app, staging, args, data = (
        _source(tmp_path)
    )
    monkeypatch.setattr(
        transfer_http, "DEFAULT_HTTP_TRANSFER_CHUNK_BYTES", len(data)
    )
    transport = _patch(monkeypatch, app, _Intercept(app, drop_one_ack=True))
    result = await transfer_http.upload_to_control(profile, config, store, args)
    assert result["bytes"] == len(data)
    assert result["http_chunks"] == 1
    assert transport.sent == [
        (f"bytes 0-{len(data) - 1}/{len(data)}", len(data)),
        (f"bytes 0-{len(data) - 1}/{len(data)}", len(data)),
    ]
    assert staging.read_bytes() == data


@pytest.mark.asyncio
async def test_repeated_proxy_413_stops_at_one_byte(tmp_path, monkeypatch):
    _workspace, config, store, profile, _gateway, app, staging, args, _data = (
        _source(tmp_path, size=8)
    )
    monkeypatch.setattr(transfer_http, "DEFAULT_HTTP_TRANSFER_CHUNK_BYTES", 8)
    transport = _patch(monkeypatch, app, _Intercept(app, max_request_bytes=0))
    with pytest.raises(RuntimeError, match="HTTP 413"):
        await transfer_http.upload_to_control(profile, config, store, args)
    assert transport.rejected == [8, 4, 2, 1]
    assert not staging.exists()


@pytest.mark.asyncio
async def test_empty_upload_preserves_legacy_single_put(tmp_path, monkeypatch):
    _workspace, config, store, profile, _gateway, app, staging, args, _data = (
        _source(tmp_path, size=0)
    )
    transport = _patch(monkeypatch, app)
    result = await transfer_http.upload_to_control(profile, config, store, args)
    assert result == {
        "bytes": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    assert transport.sent == [(None, 0)]
    assert staging.read_bytes() == b""
