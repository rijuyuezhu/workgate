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

    monkeypatch.setattr(transfer_http, "_client", client)


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
