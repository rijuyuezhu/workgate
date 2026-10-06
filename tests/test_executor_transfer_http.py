import hashlib

import httpx
import pytest
from starlette.applications import Starlette

import workgate.executor.transfer_http as transfer_http
from tests.helpers import build_tool_session_store
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.control.executor_transport import ExecutorTransportError
from workgate.control.payload_store import PayloadStore
from workgate.control.transfer_gateway import ControlTransferGateway
from workgate.executor.profile import ExecutorProfile
from workgate.executor.transfer import (
    TransferContext,
    transfer_begin_write,
    transfer_finish_write,
    transfer_write_bytes,
)
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.ids import new_executor_id, new_session_id


class _GatewayAuth:
    def __init__(self, executor_id: str, credential: str) -> None:
        self.executor_id = executor_id
        self.credential = credential

    def authenticate_live_bearer(self, bearer: str) -> str:
        if bearer != self.credential:
            raise ExecutorTransportError(
                ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
                "unauthorized",
            )
        return self.executor_id


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "executor-state",
        data_dir=tmp_path / "executor-data",
        agent_bridge_enabled=False,
    )
    config = resolve_executor_config(settings)
    store = build_tool_session_store(settings)
    executor_id = str(new_executor_id())
    credential = new_executor_credential()
    profile = ExecutorProfile(
        control_url="http://127.0.0.1",
        executor_id=executor_id,
        credential=credential,
    )
    payloads = PayloadStore(tmp_path / "control-data")
    gateway = ControlTransferGateway(
        _GatewayAuth(executor_id, credential),  # type: ignore[arg-type]
        payloads,
    )
    app = Starlette(routes=gateway.routes())
    return workspace, config, store, profile, payloads, gateway, app


def _patch_client(monkeypatch, app):
    def client(profile: ExecutorProfile) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=profile.control_url,
            headers={"Authorization": f"Bearer {profile.credential}"},
            follow_redirects=False,
        )

    monkeypatch.setattr(transfer_http, "_control_client", client)


@pytest.mark.asyncio
async def test_executor_raw_upload_streams_bound_source_to_gateway(
    tmp_path, monkeypatch
):
    workspace, config, store, profile, payloads, gateway, app = _runtime(
        tmp_path
    )
    _patch_client(monkeypatch, app)
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    data = (b"raw-upload-" * 200_000) + b"tail"
    (workspace / "source.bin").write_bytes(data)
    staging = payloads.new_staging_path("transfer")
    lease = gateway.issue_upload(
        executor_id=str(profile.executor_id),
        transfer_id="copy_" + "a" * 22,
        staging_path=staging,
        expected_bytes=len(data),
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )

    result = await transfer_http.upload_to_control(
        profile,
        config,
        store,
        {
            "session_id": session_id,
            "path": "source.bin",
            "workdir": str(workspace),
            "expected_bytes": len(data),
            "expected_sha256": hashlib.sha256(data).hexdigest(),
            "capability_path": lease.path,
            "capability_token": lease.token,
        },
    )

    assert result == {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert staging.read_bytes() == data


@pytest.mark.asyncio
async def test_executor_raw_upload_rejects_source_size_change_before_http(
    tmp_path, monkeypatch
):
    workspace, config, store, profile, _payloads, _gateway, app = _runtime(
        tmp_path
    )
    _patch_client(monkeypatch, app)
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    data = b"source-size"
    (workspace / "source.bin").write_bytes(data)

    with pytest.raises(ValueError, match="source size changed"):
        await transfer_http.upload_to_control(
            profile,
            config,
            store,
            {
                "session_id": session_id,
                "path": "source.bin",
                "expected_bytes": len(data) + 1,
                "expected_sha256": hashlib.sha256(data).hexdigest(),
                "capability_path": "/executor/v1/transfer/unused",
                "capability_token": "unused",
            },
        )


@pytest.mark.asyncio
async def test_executor_raw_download_rejects_invalid_resume_offset_before_http(
    tmp_path, monkeypatch
):
    workspace, config, store, profile, _payloads, _gateway, app = _runtime(
        tmp_path
    )
    _patch_client(monkeypatch, app)
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)

    with pytest.raises(ValueError, match="offset is invalid"):
        await transfer_http.download_from_control(
            profile,
            config,
            store,
            {
                "session_id": session_id,
                "path": "destination.bin",
                "transfer_id": "copy_" + "z" * 22,
                "expected_bytes": 10,
                "offset": 11,
                "capability_path": "/executor/v1/transfer/unused",
                "capability_token": "unused",
            },
        )


@pytest.mark.asyncio
async def test_executor_raw_upload_rejects_http_error(tmp_path, monkeypatch):
    workspace, config, store, profile, _payloads, _gateway, _app = _runtime(
        tmp_path
    )
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    data = b"upload-error"
    (workspace / "source.bin").write_bytes(data)

    def client(_profile: ExecutorProfile) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"error": "unavailable"})
        )
        return httpx.AsyncClient(
            transport=transport,
            base_url=profile.control_url,
        )

    monkeypatch.setattr(transfer_http, "_control_client", client)
    with pytest.raises(RuntimeError, match="rejected with HTTP 503"):
        await transfer_http.upload_to_control(
            profile,
            config,
            store,
            {
                "session_id": session_id,
                "path": "source.bin",
                "expected_bytes": len(data),
                "expected_sha256": hashlib.sha256(data).hexdigest(),
                "capability_path": "/executor/v1/transfer/test",
                "capability_token": "test-token",
            },
        )


@pytest.mark.asyncio
async def test_executor_raw_upload_rejects_invalid_ack(tmp_path, monkeypatch):
    workspace, config, store, profile, _payloads, _gateway, _app = _runtime(
        tmp_path
    )
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    data = b"upload-ack"
    (workspace / "source.bin").write_bytes(data)

    def client(_profile: ExecutorProfile) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "bytes": len(data) - 1,
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
        )
        return httpx.AsyncClient(
            transport=transport,
            base_url=profile.control_url,
        )

    monkeypatch.setattr(transfer_http, "_control_client", client)
    with pytest.raises(RuntimeError, match="acknowledgement is invalid"):
        await transfer_http.upload_to_control(
            profile,
            config,
            store,
            {
                "session_id": session_id,
                "path": "source.bin",
                "expected_bytes": len(data),
                "expected_sha256": hashlib.sha256(data).hexdigest(),
                "capability_path": "/executor/v1/transfer/test",
                "capability_token": "test-token",
            },
        )


@pytest.mark.asyncio
async def test_executor_raw_download_rejects_http_error(tmp_path, monkeypatch):
    workspace, config, store, profile, _payloads, _gateway, _app = _runtime(
        tmp_path
    )
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)

    def client(_profile: ExecutorProfile) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(503, content=b"unavailable")
        )
        return httpx.AsyncClient(
            transport=transport,
            base_url=profile.control_url,
        )

    monkeypatch.setattr(transfer_http, "_control_client", client)
    with pytest.raises(RuntimeError, match="rejected with HTTP 503"):
        await transfer_http.download_from_control(
            profile,
            config,
            store,
            {
                "session_id": session_id,
                "path": "destination.bin",
                "transfer_id": "copy_" + "y" * 22,
                "expected_bytes": 0,
                "offset": 0,
                "capability_path": "/executor/v1/transfer/test",
                "capability_token": "test-token",
            },
        )


@pytest.mark.asyncio
async def test_executor_raw_download_interruption_resumes_with_new_capability(
    tmp_path, monkeypatch
):
    workspace, config, store, profile, payloads, gateway, app = _runtime(
        tmp_path
    )
    destination = workspace / "destination"
    destination.mkdir()
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=destination)
    data = (b"interrupted-download-" * 100_000) + b"tail"
    sha256 = hashlib.sha256(data).hexdigest()
    transfer_id = "copy_" + "i" * 22
    context = TransferContext(config, store)
    begin = transfer_begin_write(
        "destination.bin",
        expected_bytes=len(data),
        transfer_id=transfer_id,
        session_id=session_id,
        context=context,
    )
    prefix = data[:131_072]

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield prefix
            raise httpx.ReadError("simulated interrupted download")

    def interrupted_client(_profile: ExecutorProfile) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={
                    "content-length": str(len(data)),
                    "x-workgate-transfer-offset": "0",
                },
                stream=InterruptedStream(),
            )
        )
        return httpx.AsyncClient(
            transport=transport,
            base_url=profile.control_url,
        )

    monkeypatch.setattr(transfer_http, "_control_client", interrupted_client)
    with pytest.raises(RuntimeError, match="control transfer download failed"):
        await transfer_http.download_from_control(
            profile,
            config,
            store,
            {
                "session_id": session_id,
                "path": "destination.bin",
                "transfer_id": begin.transfer_id,
                "expected_bytes": len(data),
                "offset": 0,
                "chunk_size": len(prefix),
                "capability_path": "/executor/v1/transfer/interrupted",
                "capability_token": "first-token",
            },
        )

    resumed = transfer_begin_write(
        "destination.bin",
        expected_bytes=len(data),
        transfer_id=transfer_id,
        session_id=session_id,
        context=context,
    )
    assert resumed.offset == len(prefix)

    staging = payloads.new_staging_path("transfer")
    with payloads.open_private_staging(staging, namespace="transfer") as handle:
        handle.write(data)
    payload = payloads.commit_staging(
        staging,
        namespace="transfer",
        size=len(data),
        sha256=sha256,
    )
    lease = gateway.issue_download(
        executor_id=str(profile.executor_id),
        transfer_id=transfer_id,
        payload_id=str(payload.payload_id),
        expected_bytes=len(data),
        expected_sha256=sha256,
        offset=resumed.offset,
    )
    _patch_client(monkeypatch, app)

    result = await transfer_http.download_from_control(
        profile,
        config,
        store,
        {
            "session_id": session_id,
            "path": "destination.bin",
            "transfer_id": transfer_id,
            "expected_bytes": len(data),
            "offset": resumed.offset,
            "chunk_size": len(prefix),
            "capability_path": lease.path,
            "capability_token": lease.token,
        },
    )
    finished = transfer_finish_write(
        "destination.bin",
        transfer_id,
        expected_bytes=len(data),
        expected_sha256=sha256,
        session_id=session_id,
        context=context,
    )

    assert result["offset"] == len(data)
    assert result["bytes"] == len(data) - len(prefix)
    assert finished.completed is True
    assert (destination / "destination.bin").read_bytes() == data


@pytest.mark.asyncio
async def test_executor_presigned_upload_streams_source_without_url_leak(
    tmp_path, monkeypatch
):
    workspace, config, store, _profile, _payloads, _gateway, _app = _runtime(
        tmp_path
    )
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    data = (b"object-upload-" * 100_000) + b"tail"
    (workspace / "source.bin").write_bytes(data)
    signed_url = "https://storage.test/object?X-Amz-Signature=secret"
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = await request.aread()
        return httpx.Response(200)

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(transfer_http, "_external_client", client)
    result = await transfer_http.upload_to_url(
        config,
        store,
        {
            "session_id": session_id,
            "path": "source.bin",
            "expected_bytes": len(data),
            "expected_sha256": hashlib.sha256(data).hexdigest(),
            "chunk_size": 128 * 1024,
            "url": signed_url,
        },
    )

    assert result == {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert seen["url"] == signed_url
    assert seen["body"] == data


@pytest.mark.asyncio
async def test_executor_presigned_upload_sanitizes_http_failure(
    tmp_path, monkeypatch
):
    workspace, config, store, _profile, _payloads, _gateway, _app = _runtime(
        tmp_path
    )
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    data = b"secret-url-error"
    (workspace / "source.bin").write_bytes(data)
    signed_url = "https://storage.test/object?X-Amz-Signature=do-not-log"

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(503, content=b"unavailable")
            )
        )

    monkeypatch.setattr(transfer_http, "_external_client", client)
    with pytest.raises(RuntimeError, match="HTTP 503") as exc:
        await transfer_http.upload_to_url(
            config,
            store,
            {
                "session_id": session_id,
                "path": "source.bin",
                "expected_bytes": len(data),
                "expected_sha256": hashlib.sha256(data).hexdigest(),
                "url": signed_url,
            },
        )

    assert "do-not-log" not in str(exc.value)
    assert "storage.test" not in str(exc.value)


@pytest.mark.asyncio
async def test_executor_presigned_download_rejects_partial_resume(
    tmp_path, monkeypatch
):
    workspace, config, store, _profile, _payloads, _gateway, _app = _runtime(
        tmp_path
    )
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)

    def client() -> httpx.AsyncClient:
        raise AssertionError("partial object-store resume must not issue HTTP")

    monkeypatch.setattr(transfer_http, "_external_client", client)
    with pytest.raises(
        transfer_http.ExecutorOperationFailure,
        match="does not resume partial destination writes",
    ) as exc:
        await transfer_http.download_from_url(
            config,
            store,
            {
                "session_id": session_id,
                "path": "destination.bin",
                "transfer_id": "copy_" + "o" * 22,
                "expected_bytes": 10,
                "offset": 5,
                "url": "https://storage.test/object?sig=secret",
            },
        )

    assert exc.value.code == "transfer_route_unavailable"


@pytest.mark.asyncio
async def test_executor_presigned_download_classifies_truncated_body_as_route_failure(
    tmp_path, monkeypatch
):
    workspace, config, store, _profile, _payloads, _gateway, _app = _runtime(
        tmp_path
    )
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    transfer_id = "copy_" + "t" * 22
    context = TransferContext(config, store)
    transfer_begin_write(
        "destination.bin",
        expected_bytes=10,
        transfer_id=transfer_id,
        session_id=session_id,
        context=context,
    )

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=b"12345")
            )
        )

    monkeypatch.setattr(transfer_http, "_external_client", client)
    with pytest.raises(
        transfer_http.ExecutorOperationFailure,
        match="external transfer download",
    ) as exc:
        await transfer_http.download_from_url(
            config,
            store,
            {
                "session_id": session_id,
                "path": "destination.bin",
                "transfer_id": transfer_id,
                "expected_bytes": 10,
                "offset": 0,
                "url": "https://storage.test/object?sig=secret",
            },
        )

    assert exc.value.code == "transfer_route_unavailable"
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_executor_raw_download_resumes_existing_transaction(
    tmp_path, monkeypatch
):
    workspace, config, store, profile, payloads, gateway, app = _runtime(
        tmp_path
    )
    _patch_client(monkeypatch, app)
    destination = workspace / "destination"
    destination.mkdir()
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=destination)
    data = (b"raw-download-" * 200_000) + b"tail"
    sha256 = hashlib.sha256(data).hexdigest()

    staging = payloads.new_staging_path("transfer")
    with payloads.open_private_staging(staging, namespace="transfer") as handle:
        handle.write(data)
    payload = payloads.commit_staging(
        staging,
        namespace="transfer",
        size=len(data),
        sha256=sha256,
    )

    context = TransferContext(config, store)
    transfer_id = "copy_" + "b" * 22
    begin = transfer_begin_write(
        "destination.bin",
        expected_bytes=len(data),
        transfer_id=transfer_id,
        workdir=str(destination),
        context=context,
    )
    prefix = data[:137]
    transfer_write_bytes(
        "destination.bin",
        transfer_id,
        0,
        prefix,
        hashlib.sha256(prefix).hexdigest(),
        workdir=str(destination),
        context=context,
    )
    lease = gateway.issue_download(
        executor_id=str(profile.executor_id),
        transfer_id=transfer_id,
        payload_id=str(payload.payload_id),
        expected_bytes=len(data),
        expected_sha256=sha256,
        offset=len(prefix),
    )

    result = await transfer_http.download_from_control(
        profile,
        config,
        store,
        {
            "session_id": session_id,
            "path": "destination.bin",
            "workdir": str(destination),
            "transfer_id": begin.transfer_id,
            "expected_bytes": len(data),
            "offset": len(prefix),
            "capability_path": lease.path,
            "capability_token": lease.token,
        },
    )
    finished = transfer_finish_write(
        "destination.bin",
        transfer_id,
        expected_bytes=len(data),
        expected_sha256=sha256,
        workdir=str(destination),
        context=context,
    )

    assert result == {
        "offset": len(data),
        "bytes": len(data) - len(prefix),
    }
    assert finished.completed is True
    assert (destination / "destination.bin").read_bytes() == data
