import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import workgate.control.session_copy as session_copy_module
from tests.helpers import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.object_store_transfer import (
    ObjectStoreRouteUnavailable,
    S3ObjectTransferService,
)
from workgate.control.payload_store import PayloadStore
from workgate.control.session_copy import (
    SESSION_COPY_MANAGED_KIND,
    ControlSessionCopyService,
)
from workgate.control.session_copy_store import TransferPayloadCapacityError
from workgate.control.state import ControlSessionRecord
from workgate.control.tool_routing import ControlToolRouter
from workgate.persistence import FileStateStore
from workgate.protocol.executor import ExecutorResult, OperationError
from workgate.protocol.ids import (
    new_command_id,
    new_executor_id,
    new_session_id,
)
from workgate.schemas.result_models.jobs import JobStartOutput


def _disabled_object_store(
    state_store: FileStateStore,
) -> S3ObjectTransferService:
    return S3ObjectTransferService(
        state_store,
        bucket=None,
        prefix="workgate",
        region=None,
        endpoint_url=None,
        presign_ttl_s=3600,
    )


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()


async def _paired_sessions(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dst").mkdir(parents=True, exist_ok=True)
    harness = build_paired_control_harness(get_settings())
    src = await harness.control.session_coordinator.start_session(
        workdir="src", executor_id=harness.executor_id
    )
    dst = await harness.control.session_coordinator.start_session(
        workdir="dst", executor_id=harness.executor_id
    )
    assert isinstance(src, dict)
    assert isinstance(dst, dict)
    return harness, str(src["session_id"]), str(dst["session_id"])


def _record_executor_ops(harness, monkeypatch) -> list[str]:
    operations: list[str] = []
    original_call = harness.call

    async def call(executor_id, op, args=None, **kwargs):
        operations.append(op)
        return await original_call(executor_id, op, args, **kwargs)

    monkeypatch.setattr(harness.control.executor_transport, "call", call)
    return operations


@pytest.mark.asyncio
async def test_shared_session_copy_keeps_file_bytes_on_same_executor(
    tmp_path, monkeypatch
):
    payload = b"abcdef" * 1000
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "payload.bin").write_bytes(payload)
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)
    operations = _record_executor_ops(harness, monkeypatch)
    observed_sessions: list[str] = []
    original_observe = (
        harness.control.session_coordinator.observe_session_activity
    )

    def observe_session_activity(session_id: str, **kwargs) -> None:
        observed_sessions.append(session_id)
        original_observe(session_id, **kwargs)

    monkeypatch.setattr(
        harness.control.session_coordinator,
        "observe_session_activity",
        observe_session_activity,
    )
    progress: list[dict[str, Any]] = []

    async def report(value: dict[str, Any]) -> None:
        progress.append(value)

    async def unexpected_object_store_reconcile() -> None:
        raise AssertionError(
            "same-executor copy must not touch object-store state"
        )

    monkeypatch.setattr(
        harness.control.session_copy_service,
        "reconcile_object_store_orphans",
        unexpected_object_store_reconcile,
    )

    result = await harness.control.session_copy_service.copy(
        src_session_id=src_id,
        src_path="payload.bin",
        dst_session_id=dst_id,
        dst_path="copied.bin",
        chunk_size=128,
        progress=report,
    )

    assert result.kind == "file"
    assert result.relation.route == "same_executor"
    assert result.relation.same_executor is True
    assert result.relation.same_session is False
    assert result.transport == "same_executor"
    assert result.bytes == len(payload)
    assert result.chunks > 1
    assert result.source.session_id == src_id
    assert result.destination.session_id == dst_id
    assert (tmp_path / "dst" / "copied.bin").read_bytes() == payload
    assert progress[0]["phase"] == "stat"
    assert progress[-1]["bytes_transferred"] == len(payload)

    assert operations.count("transfer_copy_file") == 1
    assert not {
        "transfer_begin_write",
        "transfer_read_chunk",
        "transfer_write_chunk",
        "transfer_finish_write",
    }.intersection(operations)

    assert src_id in observed_sessions
    assert dst_id in observed_sessions


@pytest.mark.asyncio
async def test_shared_session_copy_packs_and_unpacks_directory(
    tmp_path, monkeypatch
):
    (tmp_path / "src" / "tree" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "tree" / "nested" / "note.txt").write_text(
        "hello", encoding="utf-8"
    )
    (tmp_path / "src" / "tree" / "data.bin").write_bytes(b"\x00\x01")
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)
    operations = _record_executor_ops(harness, monkeypatch)

    result = await harness.control.session_copy_service.copy(
        src_session_id=src_id,
        src_path="tree",
        dst_session_id=dst_id,
        dst_path="tree-copy",
        kind="dir",
        chunk_size=256,
    )

    assert result.kind == "dir"
    assert result.archive_bytes is not None and result.archive_bytes > 0
    assert result.entries is not None and result.entries >= 2
    assert result.cleanup_errors == []
    assert (tmp_path / "dst" / "tree-copy" / "nested" / "note.txt").read_text(
        encoding="utf-8"
    ) == "hello"
    assert (
        tmp_path / "dst" / "tree-copy" / "data.bin"
    ).read_bytes() == b"\x00\x01"

    assert operations.count("transfer_pack_dir") == 1
    assert operations.count("transfer_unpack_archive") == 1
    assert "transfer_alloc_temp_path" not in operations
    assert not {
        "transfer_begin_write",
        "transfer_read_chunk",
        "transfer_write_chunk",
        "transfer_finish_write",
    }.intersection(operations)


@pytest.mark.asyncio
async def test_shared_session_copy_rejects_kind_mismatch(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "payload.txt").write_text("file", encoding="utf-8")
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="does not match source type"):
        await harness.control.session_copy_service.copy(
            src_session_id=src_id,
            src_path="payload.txt",
            dst_session_id=dst_id,
            dst_path="payload-copy",
            kind="dir",
        )


@pytest.mark.asyncio
async def test_shared_session_copy_preserves_destination_when_overwrite_is_false(
    tmp_path, monkeypatch
):
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dst").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "payload.txt").write_text("new", encoding="utf-8")
    (tmp_path / "dst" / "payload.txt").write_text("old", encoding="utf-8")
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="transfer_copy_file failed"):
        await harness.control.session_copy_service.copy(
            src_session_id=src_id,
            src_path="payload.txt",
            dst_session_id=dst_id,
            dst_path="payload.txt",
            overwrite=False,
        )

    assert (tmp_path / "dst" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "old"


def test_copy_binding_requires_confirmed_workdir() -> None:
    record = ControlSessionRecord(
        session_id=new_session_id(),
        executor_id=new_executor_id(),
        workdir=None,
        status="active",
        created_at=1,
        updated_at=1,
    )

    with pytest.raises(RuntimeError, match="no confirmed workdir"):
        ControlSessionCopyService._binding_snapshot(record)


@pytest.mark.asyncio
async def test_managed_copy_rejects_changed_session_binding(
    tmp_path, monkeypatch
):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "payload.txt").write_text("data", encoding="utf-8")
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="source session binding changed"):
        await harness.control.session_copy_service.copy(
            src_session_id=src_id,
            src_path="payload.txt",
            dst_session_id=dst_id,
            dst_path="payload.txt",
            expected_bindings={
                src_id: {
                    "executor_id": harness.executor_id,
                    "workdir": "/stale",
                }
            },
        )


@pytest.mark.asyncio
async def test_background_copy_snapshots_bindings_before_launch(
    tmp_path, monkeypatch
):
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_start(
        session_id: str,
        managed_kind: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> JobStartOutput:
        captured.update(
            session_id=session_id,
            managed_kind=managed_kind,
            payload=payload,
            kwargs=kwargs,
        )
        return JobStartOutput(
            job_id="job_copy",
            kind="managed",
            name="copy-result.bin",
            status="running",
            command="session_copy",
            cwd=".",
            session_id=src_id,
            created_at=1.0,
            updated_at=1.0,
            attempts=1,
        )

    monkeypatch.setattr(
        session_copy_module,
        "start_managed_job_without_session_admission",
        fake_start,
    )

    started = await harness.control.session_copy_service.start_background(
        src_session_id=src_id,
        src_path="payload.bin",
        dst_session_id=dst_id,
        dst_path="result.bin",
        chunk_size=1234,
    )

    assert started.job_id == "job_copy"
    assert captured["session_id"] == src_id
    assert captured["managed_kind"] == SESSION_COPY_MANAGED_KIND
    payload = captured["payload"]
    assert payload["src_session_id"] == src_id
    assert payload["dst_session_id"] == dst_id
    assert payload["bindings"][src_id]["executor_id"] == harness.executor_id
    assert payload["bindings"][dst_id]["executor_id"] == harness.executor_id
    assert payload["chunk_size"] >= 1234


def test_session_copy_managed_registration_is_runtime_bound() -> None:
    service = object.__new__(ControlSessionCopyService)
    kind, handler = service.managed_job_registration()

    assert kind == SESSION_COPY_MANAGED_KIND
    assert handler == service._run_managed_job


class _CheckpointSessions:
    def __init__(self, records: tuple[ControlSessionRecord, ...]) -> None:
        self.records = {str(record.session_id): record for record in records}
        self.require_available: tuple[str, ...] | None = None
        self.availability = "missing_on_executor"

    @asynccontextmanager
    async def session_admission(
        self,
        session_ids: tuple[str, ...],
        *,
        require_available: tuple[str, ...] | None = None,
    ):
        self.require_available = require_available
        yield tuple(self.records[session_id] for session_id in session_ids)

    def observe_session_activity(self, session_id: str) -> None:
        _ = session_id

    async def reconcile_session_activity_after_error(
        self, record: ControlSessionRecord
    ) -> None:
        _ = record

    async def session_availability(self, session_id: str) -> str:
        assert session_id in self.records
        return self.availability


class _RawGatewayStub:
    def __init__(self, data_dir: Path, *, upload_bytes: bytes = b"") -> None:
        self.payloads = PayloadStore(data_dir)
        self.upload_bytes = upload_bytes
        self.upload_staging: Path | None = None
        self.upload_expected_bytes = 0
        self.upload_expected_sha256 = ""
        self.download_payload_id: str | None = None
        self.download_expected_bytes = 0
        self.download_expected_sha256 = ""
        self.download_offset = 0

    def issue_upload(
        self,
        *,
        executor_id: str,
        transfer_id: str,
        staging_path: Path,
        expected_bytes: int,
        expected_sha256: str,
    ):
        _ = (executor_id, transfer_id)
        self.upload_staging = staging_path
        self.upload_expected_bytes = expected_bytes
        self.upload_expected_sha256 = expected_sha256
        return SimpleNamespace(
            capability_id="xfer_upload",
            path="/executor/v1/transfer/xfer_upload",
            token="upload-token",
        )

    def issue_download(
        self,
        *,
        executor_id: str,
        transfer_id: str,
        payload_id: str,
        expected_bytes: int,
        expected_sha256: str,
        offset: int,
    ):
        _ = (executor_id, transfer_id)
        self.download_payload_id = payload_id
        self.download_expected_bytes = expected_bytes
        self.download_expected_sha256 = expected_sha256
        self.download_offset = offset
        return SimpleNamespace(
            capability_id="xfer_download",
            path="/executor/v1/transfer/xfer_download",
            token="download-token",
        )

    def revoke(self, capability_id: str) -> None:
        _ = capability_id

    def complete_upload(self) -> dict[str, Any]:
        staging = self.upload_staging
        assert staging is not None
        raw = self.upload_bytes
        digest = hashlib.sha256(raw).hexdigest()
        if (
            len(raw) != self.upload_expected_bytes
            or digest != self.upload_expected_sha256
        ):
            raise RuntimeError("raw upload integrity mismatch")
        with self.payloads.open_private_staging(
            staging, namespace="transfer"
        ) as handle:
            handle.write(raw)
        return {"bytes": len(raw), "sha256": digest}

    def download_bytes(self) -> bytes:
        payload_id = self.download_payload_id
        assert payload_id is not None
        handle, _path = self.payloads.open_payload(
            payload_id,
            namespace="transfer",
            size=self.download_expected_bytes,
            sha256=self.download_expected_sha256,
        )
        try:
            handle.seek(self.download_offset)
            return handle.read()
        finally:
            handle.close()


class _DestinationOnlyTransport:
    def __init__(
        self,
        source_executor_id: str,
        destination_executor_id: str,
        gateway: _RawGatewayStub,
    ) -> None:
        self.source_executor_id = source_executor_id
        self.destination_executor_id = destination_executor_id
        self.gateway = gateway
        self.calls: list[str] = []
        self.call_details: list[
            tuple[str, dict[str, Any], str | None, float | None]
        ] = []
        self.data = bytearray()
        self.fail_release_receipts = False
        self.fail_abandon_import = False
        self.abandon_safe_to_forget = True

    async def call(
        self,
        executor_id: str,
        op: str,
        args: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
    ) -> ExecutorResult:
        _ = (session_id, timeout_s)
        if executor_id == self.source_executor_id:
            raise AssertionError(
                "source executor must not be contacted after export"
            )
        assert executor_id == self.destination_executor_id
        values = args or {}
        self.calls.append(op)
        self.call_details.append((op, dict(values), session_id, timeout_s))
        if op == "transfer_begin_write":
            result: Any = {
                "path": str(values["path"]),
                "temp_path": ".temporary",
                "transfer_id": str(values["transfer_id"]),
                "created": True,
                "expected_bytes": int(values["expected_bytes"]),
                "offset": 0,
                "resumed": False,
                "completed": False,
                "sha256": None,
            }
        elif op == "transfer.http_download":
            assert int(values["offset"]) == len(self.data)
            raw = self.gateway.download_bytes()
            self.data.extend(raw)
            result = {
                "offset": len(self.data),
                "bytes": len(raw),
            }
        elif op == "transfer_finish_write":
            result = {
                "path": str(values["path"]),
                "bytes": len(self.data),
                "sha256": hashlib.sha256(self.data).hexdigest(),
                "completed": True,
            }
        elif op == "transfer_release_receipts":
            if self.fail_release_receipts:
                raise RuntimeError("simulated receipt release failure")
            result = {
                "write_receipt_deleted": True,
                "unpack_receipt_deleted": True,
            }
        elif op == "transfer_abandon_import":
            if self.fail_abandon_import:
                raise RuntimeError("simulated abandon failure")
            assert values["import_path"]
            result = {
                "safe_to_forget": self.abandon_safe_to_forget,
                "write_reconciled": True,
                "unpack_reconciled": False,
            }
        else:
            raise AssertionError(f"unexpected destination operation: {op}")
        return ExecutorResult(id=new_command_id(), ok=True, result=result)


def _checkpoint_service(
    tmp_path: Path,
    payload: bytes,
    *,
    owner_job_id: str | None = None,
    object_store: Any | None = None,
):
    source_executor_id = str(new_executor_id())
    destination_executor_id = str(new_executor_id())
    source_session_id = str(new_session_id())
    destination_session_id = str(new_session_id())
    source = ControlSessionRecord(
        session_id=source_session_id,
        executor_id=source_executor_id,
        workdir="src",
        status="active",
        created_at=1.0,
        updated_at=1.0,
    )
    destination = ControlSessionRecord(
        session_id=destination_session_id,
        executor_id=destination_executor_id,
        workdir="dst",
        status="active",
        created_at=1.0,
        updated_at=1.0,
    )
    sessions = _CheckpointSessions((source, destination))
    gateway = _RawGatewayStub(tmp_path / "data")
    transport = _DestinationOnlyTransport(
        source_executor_id, destination_executor_id, gateway
    )
    state_store = FileStateStore(lambda: tmp_path / "state")
    if owner_job_id is not None:
        state_store.write_json(
            state_store.layout.jobs_store_path,
            {
                "version": 2,
                "jobs": [{"job_id": owner_job_id, "status": "retrying"}],
            },
        )
    service = ControlSessionCopyService(
        sessions,  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        state_store,
        tmp_path / "data",
        transfer_gateway=gateway,  # type: ignore[arg-type]
        object_store=(
            object_store
            if object_store is not None
            else _disabled_object_store(state_store)
        ),  # type: ignore[arg-type]
        max_transfer_payload_bytes=10_000_000,
        max_transfer_payload_store_bytes=10_000_000,
    )
    transfer_id = "copy_" + "a" * 22
    staging = service._payloads.new_staging_path("transfer")
    with service._payloads.open_private_staging(
        staging, namespace="transfer"
    ) as handle:
        handle.write(payload)
    service._checkpoints.reserve_export(
        transfer_id=transfer_id,
        owner_job_id=owner_job_id,
        payload_size=len(payload),
        max_payload_bytes=10_000_000,
        max_store_bytes=10_000_000,
    )
    checkpoint = service._checkpoints.commit_export(
        checkpoint={
            "transfer_id": transfer_id,
            "owner_job_id": owner_job_id,
            "source_session_id": source_session_id,
            "source_executor_id": source_executor_id,
            "destination_session_id": destination_session_id,
            "destination_executor_id": destination_executor_id,
            "source_path": "source.bin",
            "destination_path": "destination.bin",
            "kind": "file",
            "overwrite": True,
            "chunk_size": 3,
            "source_resolved_path": "source.bin",
            "last_known_step": "exported",
        },
        staging_path=staging,
        payload_size=len(payload),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return service, sessions, transport, checkpoint, state_store


@pytest.mark.asyncio
async def test_cross_executor_retry_uses_control_relay_with_source_offline(
    tmp_path,
):
    payload = b"durable-control-payload"
    owner_job_id = "job_" + "a" * 12
    service, sessions, transport, checkpoint, state_store = _checkpoint_service(
        tmp_path, payload, owner_job_id=owner_job_id
    )
    transport.fail_release_receipts = True

    result = await service.copy(
        src_session_id=str(checkpoint.source_session_id),
        src_path=checkpoint.source_path,
        dst_session_id=str(checkpoint.destination_session_id),
        dst_path=checkpoint.destination_path,
        kind="file",
        overwrite=True,
        chunk_size=checkpoint.chunk_size,
        transfer_id=checkpoint.transfer_id,
        owner_job_id=owner_job_id,
    )

    assert sessions.require_available == (
        str(checkpoint.destination_session_id),
    )
    assert transport.data == payload
    import_calls = [
        detail
        for detail in transport.call_details
        if detail[0]
        in {
            "transfer_begin_write",
            "transfer.http_download",
            "transfer_finish_write",
        }
    ]
    assert import_calls
    for _op, args, session_id, _timeout_s in import_calls:
        assert "workdir" not in args
        assert session_id == str(checkpoint.destination_session_id)
    assert result.transport == "control_relay"
    assert result.bytes == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    imported = service._checkpoints.load(checkpoint.transfer_id)
    assert imported is not None
    assert imported.last_known_step == "imported"
    assert imported.receipts_released is False
    payload_path = service._payloads.path(
        checkpoint.payload_id, namespace="transfer"
    )
    assert payload_path.exists()

    state_store.write_json(
        state_store.layout.jobs_store_path,
        {
            "version": 2,
            "jobs": [{"job_id": owner_job_id, "status": "succeeded"}],
        },
    )
    await service.reconcile_abandonments()
    assert service._checkpoints.load(checkpoint.transfer_id) is None
    assert not payload_path.exists()
    assert "transfer_abandon_import" in transport.calls


@pytest.mark.asyncio
async def test_cross_executor_import_resumes_after_control_service_restart(
    tmp_path,
):
    payload = b"restart-after-export"
    service, sessions, _transport, checkpoint, state_store = (
        _checkpoint_service(tmp_path, payload)
    )
    await service.aclose()

    gateway = _RawGatewayStub(tmp_path / "data")
    transport = _DestinationOnlyTransport(
        str(checkpoint.source_executor_id),
        str(checkpoint.destination_executor_id),
        gateway,
    )
    restored = ControlSessionCopyService(
        sessions,  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        state_store,
        tmp_path / "data",
        transfer_gateway=gateway,  # type: ignore[arg-type]
        object_store=_disabled_object_store(state_store),
        max_transfer_payload_bytes=10_000_000,
        max_transfer_payload_store_bytes=10_000_000,
    )

    result = await restored.copy(
        src_session_id=str(checkpoint.source_session_id),
        src_path=checkpoint.source_path,
        dst_session_id=str(checkpoint.destination_session_id),
        dst_path=checkpoint.destination_path,
        kind="file",
        overwrite=True,
        chunk_size=checkpoint.chunk_size,
        transfer_id=checkpoint.transfer_id,
    )

    assert result.transport == "control_relay"
    assert result.bytes == len(payload)
    assert transport.data == payload
    assert "transfer.http_download" in transport.calls
    assert "transfer.http_upload" not in transport.calls
    await restored.aclose()


@pytest.mark.asyncio
async def test_retention_abandonment_keeps_tombstone_until_executor_reconciles(
    tmp_path,
):
    payload = b"retained-until-safe-abandon"
    owner_job_id = "job_" + "d" * 12
    service, _sessions, transport, checkpoint, state_store = (
        _checkpoint_service(tmp_path, payload, owner_job_id=owner_job_id)
    )
    checkpoint = service._checkpoints.update(
        checkpoint.transfer_id,
        import_path=checkpoint.destination_path,
        import_resource_id=checkpoint.transfer_id,
        last_known_step="importing",
    )
    payload_path = service._payloads.path(
        checkpoint.payload_id, namespace="transfer"
    )
    state_store.write_json(
        state_store.layout.jobs_store_path,
        {"version": 2, "jobs": []},
    )
    transport.fail_abandon_import = True

    await service.reconcile_abandonments()

    retained = service._checkpoints.load(checkpoint.transfer_id)
    assert retained is not None
    assert retained.abandoning is True
    assert retained.payload_retained is False
    assert not payload_path.exists()
    assert transport.calls == ["transfer_abandon_import"]
    op, args, session_id, timeout_s = transport.call_details[-1]
    assert op == "transfer_abandon_import"
    assert "workdir" not in args
    assert session_id is None
    assert timeout_s == session_copy_module._ABANDONMENT_RPC_TIMEOUT_S

    transport.fail_abandon_import = False
    await service.reconcile_abandonments(
        executor_id=str(checkpoint.destination_executor_id)
    )

    assert service._checkpoints.load(checkpoint.transfer_id) is None
    assert transport.calls == [
        "transfer_abandon_import",
        "transfer_abandon_import",
    ]


@pytest.mark.asyncio
async def test_scheduled_abandonment_reruns_after_overlapping_hello(
    tmp_path, monkeypatch
):
    service, _sessions, _transport, checkpoint, _state_store = (
        _checkpoint_service(tmp_path, b"rerun-after-timeout")
    )
    executor_id = str(checkpoint.destination_executor_id)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    calls: list[str | None] = []

    async def reconcile(*, executor_id: str | None = None) -> None:
        calls.append(executor_id)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
            raise TimeoutError("simulated stale offered cleanup")
        second_started.set()

    monkeypatch.setattr(service, "reconcile_abandonments", reconcile)
    service.schedule_reconcile_abandonments(executor_id=executor_id)
    await asyncio.wait_for(first_started.wait(), timeout=0.5)

    # A reconnect hello arriving while the old offered cleanup is still pending
    # must request one bounded rerun instead of being lost to task coalescing.
    service.schedule_reconcile_abandonments(executor_id=executor_id)
    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=0.5)
    await asyncio.sleep(0)

    assert calls == [executor_id, executor_id]
    await service.aclose()


@pytest.mark.asyncio
async def test_abandonment_keeps_tombstone_until_executor_confirms_safe_cleanup(
    tmp_path,
):
    payload = b"retain-authority-until-safe"
    owner_job_id = "job_" + "e" * 12
    service, _sessions, transport, checkpoint, state_store = (
        _checkpoint_service(tmp_path, payload, owner_job_id=owner_job_id)
    )
    checkpoint = service._checkpoints.update(
        checkpoint.transfer_id,
        import_path=checkpoint.destination_path,
        import_resource_id=checkpoint.transfer_id,
        last_known_step="importing",
    )
    state_store.write_json(
        state_store.layout.jobs_store_path,
        {"version": 2, "jobs": []},
    )
    transport.abandon_safe_to_forget = False

    await service.reconcile_abandonments()

    retained = service._checkpoints.load(checkpoint.transfer_id)
    assert retained is not None
    assert retained.abandoning is True
    assert retained.payload_retained is False

    transport.abandon_safe_to_forget = True
    await service.reconcile_abandonments(
        executor_id=str(checkpoint.destination_executor_id)
    )
    assert service._checkpoints.load(checkpoint.transfer_id) is None


@pytest.mark.asyncio
async def test_corrupt_control_payload_fails_without_destination_publish(
    tmp_path,
):
    service, _sessions, transport, checkpoint, _state_store = (
        _checkpoint_service(tmp_path, b"payload")
    )
    payload_path = service._payloads.path(
        checkpoint.payload_id, namespace="transfer"
    )
    payload_path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="changed"):
        await service.copy(
            src_session_id=str(checkpoint.source_session_id),
            src_path=checkpoint.source_path,
            dst_session_id=str(checkpoint.destination_session_id),
            dst_path=checkpoint.destination_path,
            kind="file",
            overwrite=True,
            chunk_size=checkpoint.chunk_size,
            transfer_id=checkpoint.transfer_id,
        )

    assert transport.data == b""
    assert transport.calls == [
        "transfer_begin_write",
        "transfer.http_download",
    ]


class _ObjectStoreStub:
    def __init__(
        self,
        *,
        enabled: bool = True,
        setup_error: Exception | None = None,
        cleanup_error: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.setup_error = setup_error
        self.cleanup_error = cleanup_error
        self.data = b""
        self.begun = 0
        self.finished = 0
        self.reconciled = 0

    async def reconcile_orphans(self) -> tuple[str, ...]:
        self.reconciled += 1
        return ()

    def begin_attempt(self, transfer_id: str):
        self.begun += 1
        if self.setup_error is not None:
            raise self.setup_error
        attempt = SimpleNamespace(
            transfer_id=transfer_id,
            bucket="bucket",
            key=f"transfers/{transfer_id}",
        )
        return attempt, "https://storage.test/upload?sig=secret"

    def presign_get(self, attempt) -> str:
        _ = attempt
        return "https://storage.test/download?sig=secret"

    async def finish_attempt(self, attempt) -> str | None:
        _ = attempt
        self.finished += 1
        return self.cleanup_error


@pytest.mark.asyncio
async def test_durable_control_checkpoint_skips_configured_object_store(
    tmp_path,
):
    payload = b"durable-route-wins"
    object_store = _ObjectStoreStub()
    service, sessions, transport, checkpoint, _state_store = (
        _checkpoint_service(
            tmp_path,
            payload,
            object_store=object_store,
        )
    )

    result = await service.copy(
        src_session_id=str(checkpoint.source_session_id),
        src_path=checkpoint.source_path,
        dst_session_id=str(checkpoint.destination_session_id),
        dst_path=checkpoint.destination_path,
        kind="file",
        overwrite=True,
        chunk_size=checkpoint.chunk_size,
        transfer_id=checkpoint.transfer_id,
    )

    assert result.transport == "control_relay"
    assert sessions.require_available == (
        str(checkpoint.destination_session_id),
    )
    assert object_store.reconciled == 1
    assert object_store.begun == 0
    assert object_store.finished == 0
    assert "transfer.http_download" in transport.calls
    assert transport.data == payload


class _FirstExportTransport:
    def __init__(
        self,
        source_executor_id: str,
        destination_executor_id: str,
        gateway: _RawGatewayStub,
        *,
        source_kind: str,
        payload: bytes,
        archive: bytes | None = None,
        object_store: _ObjectStoreStub | None = None,
        object_download_failures: int = 0,
        object_semantic_failure: bool = False,
    ) -> None:
        self.source_executor_id = source_executor_id
        self.destination_executor_id = destination_executor_id
        self.gateway = gateway
        self.source_kind = source_kind
        self.payload = payload
        self.archive = archive
        self.object_store = object_store
        self.object_download_failures = object_download_failures
        self.object_semantic_failure = object_semantic_failure
        self.destination = bytearray()
        self.calls: list[tuple[str, str]] = []
        self.allocated_path = ".incoming.tar.gz"

    async def call(
        self,
        executor_id: str,
        op: str,
        args: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
    ) -> ExecutorResult:
        _ = (session_id, timeout_s)
        values = args or {}
        self.calls.append((executor_id, op))
        if executor_id == self.source_executor_id:
            if op == "transfer_stat":
                if self.source_kind == "file":
                    result: Any = {
                        "path": str(values["path"]),
                        "type": "file",
                        "size": len(self.payload),
                        "modified": 1.0,
                        "sha256": hashlib.sha256(self.payload).hexdigest(),
                    }
                else:
                    result = {
                        "path": str(values["path"]),
                        "type": "dir",
                        "size": None,
                        "modified": 1.0,
                        "sha256": None,
                    }
            elif op == "transfer_pack_dir":
                assert self.archive is not None
                result = {
                    "path": str(values["path"]),
                    "archive_path": ".source.tar.gz",
                    "bytes": len(self.archive),
                    "sha256": hashlib.sha256(self.archive).hexdigest(),
                    "compression": "gz",
                }
            elif op == "transfer.http_upload":
                result = self.gateway.complete_upload()
            elif op == "transfer.url_upload":
                assert self.object_store is not None
                raw = (
                    self.payload
                    if self.source_kind == "file"
                    else (self.archive or b"")
                )
                self.object_store.data = raw
                result = {
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            elif op == "transfer_delete_temp_path":
                result = {"path": str(values["path"]), "deleted": True}
            else:
                raise AssertionError(f"unexpected source operation: {op}")
        else:
            assert executor_id == self.destination_executor_id
            if op == "transfer_alloc_temp_path":
                result = {"path": self.allocated_path}
            elif op == "transfer_begin_write":
                result = {
                    "path": str(values["path"]),
                    "temp_path": ".temporary",
                    "transfer_id": str(values["transfer_id"]),
                    "created": True,
                    "expected_bytes": int(values["expected_bytes"]),
                    "offset": len(self.destination),
                    "resumed": bool(self.destination),
                    "completed": False,
                    "sha256": None,
                }
            elif op == "transfer.http_download":
                assert int(values["offset"]) == len(self.destination)
                raw = self.gateway.download_bytes()
                self.destination.extend(raw)
                result = {
                    "offset": len(self.destination),
                    "bytes": len(raw),
                }
            elif op == "transfer.url_download":
                assert self.object_store is not None
                if self.object_semantic_failure:
                    return ExecutorResult(
                        id=new_command_id(),
                        ok=False,
                        error=OperationError(
                            code="operation_failed",
                            message=(
                                "destination write failed "
                                "https://storage.test/object?sig=do-not-leak"
                            ),
                        ),
                    )
                offset = int(values["offset"])
                assert offset == len(self.destination)
                if self.object_download_failures:
                    self.object_download_failures -= 1
                    remaining = self.object_store.data[offset:]
                    prefix = remaining[: max(1, len(remaining) // 3)]
                    self.destination.extend(prefix)
                    return ExecutorResult(
                        id=new_command_id(),
                        ok=False,
                        error=OperationError(
                            code="transfer_route_unavailable",
                            message=(
                                "object-store download interrupted "
                                "https://storage.test/object?sig=do-not-leak"
                            ),
                        ),
                    )
                raw = self.object_store.data[offset:]
                self.destination.extend(raw)
                result = {
                    "offset": len(self.destination),
                    "bytes": len(raw),
                }
            elif op == "transfer_abandon_import":
                self.destination.clear()
                result = {
                    "safe_to_forget": True,
                    "write_reconciled": True,
                    "unpack_reconciled": self.source_kind == "dir",
                }
            elif op == "transfer_finish_write":
                result = {
                    "path": str(values["path"]),
                    "bytes": len(self.destination),
                    "sha256": hashlib.sha256(self.destination).hexdigest(),
                    "completed": True,
                }
            elif op == "transfer_unpack_archive":
                result = {
                    "path": str(values["dst_path"]),
                    "archive_path": str(values["archive_path"]),
                    "entries": 3,
                    "completed": True,
                    "archive_deleted": True,
                    "backup_deleted": True,
                    "cleanup_errors": [],
                    "resumed": False,
                }
            elif op == "transfer_release_receipts":
                result = {
                    "write_receipt_deleted": True,
                    "unpack_receipt_deleted": self.source_kind == "dir",
                }
            else:
                raise AssertionError(f"unexpected destination operation: {op}")
        return ExecutorResult(id=new_command_id(), ok=True, result=result)


def _fresh_cross_executor_service(
    tmp_path: Path,
    *,
    source_kind: str,
    payload: bytes,
    archive: bytes | None = None,
    max_payload_bytes: int = 10_000_000,
    max_store_bytes: int = 10_000_000,
    object_store: _ObjectStoreStub | None = None,
    object_download_failures: int = 0,
    object_semantic_failure: bool = False,
):
    source_executor_id = str(new_executor_id())
    destination_executor_id = str(new_executor_id())
    source = ControlSessionRecord(
        session_id=str(new_session_id()),
        executor_id=source_executor_id,
        workdir="src",
        status="active",
        created_at=1.0,
        updated_at=1.0,
    )
    destination = ControlSessionRecord(
        session_id=str(new_session_id()),
        executor_id=destination_executor_id,
        workdir="dst",
        status="active",
        created_at=1.0,
        updated_at=1.0,
    )
    sessions = _CheckpointSessions((source, destination))
    gateway = _RawGatewayStub(
        tmp_path / "data",
        upload_bytes=payload if source_kind == "file" else (archive or b""),
    )
    transport = _FirstExportTransport(
        source_executor_id,
        destination_executor_id,
        gateway,
        source_kind=source_kind,
        payload=payload,
        archive=archive,
        object_store=object_store,
        object_download_failures=object_download_failures,
        object_semantic_failure=object_semantic_failure,
    )
    state_store = FileStateStore(lambda: tmp_path / "state")
    service = ControlSessionCopyService(
        sessions,  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        state_store,
        tmp_path / "data",
        transfer_gateway=gateway,  # type: ignore[arg-type]
        object_store=(
            object_store
            if object_store is not None
            else _disabled_object_store(state_store)
        ),  # type: ignore[arg-type]
        max_transfer_payload_bytes=max_payload_bytes,
        max_transfer_payload_store_bytes=max_store_bytes,
    )
    return service, source, destination, transport


@pytest.mark.asyncio
async def test_cross_executor_object_store_file_bypasses_control_payload(
    tmp_path,
):
    payload = b"object-store-file" * 100
    object_store = _ObjectStoreStub()
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
        object_store=object_store,
    )

    result = await service.copy(
        src_session_id=str(source.session_id),
        src_path="source.bin",
        dst_session_id=str(destination.session_id),
        dst_path="destination.bin",
        kind="file",
        overwrite=False,
        chunk_size=64,
    )

    operations = [op for _executor_id, op in transport.calls]
    assert result.transport == "object_store"
    assert result.fallbacks == []
    assert result.bytes == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert bytes(transport.destination) == payload
    assert "transfer.url_upload" in operations
    assert "transfer.url_download" in operations
    assert "transfer.http_upload" not in operations
    assert "transfer.http_download" not in operations
    assert not list(
        service._payloads.directory("transfer").glob("payload_*.bin")
    )
    assert object_store.finished == 1


@pytest.mark.asyncio
async def test_cross_executor_object_store_directory_packs_once(tmp_path):
    archive = b"packed-directory" * 100
    object_store = _ObjectStoreStub()
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="dir",
        payload=b"",
        archive=archive,
        object_store=object_store,
    )

    result = await service.copy(
        src_session_id=str(source.session_id),
        src_path="tree",
        dst_session_id=str(destination.session_id),
        dst_path="tree-copy",
        kind="dir",
        overwrite=True,
        chunk_size=64,
    )

    source_ops = [
        op
        for executor_id, op in transport.calls
        if executor_id == transport.source_executor_id
    ]
    destination_ops = [
        op
        for executor_id, op in transport.calls
        if executor_id == transport.destination_executor_id
    ]
    assert result.transport == "object_store"
    assert result.archive_bytes == len(archive)
    assert result.archive_sha256 == hashlib.sha256(archive).hexdigest()
    assert result.entries == 3
    assert source_ops.count("transfer_pack_dir") == 1
    assert "transfer.url_upload" in source_ops
    assert "transfer.http_upload" not in source_ops
    assert "transfer.url_download" in destination_ops
    assert "transfer.http_download" not in destination_ops
    assert "transfer_unpack_archive" in destination_ops
    assert source_ops.count("transfer_delete_temp_path") == 1


@pytest.mark.asyncio
async def test_cross_executor_object_store_unavailable_falls_back_to_control(
    tmp_path,
):
    payload = b"fallback-control"
    object_store = _ObjectStoreStub(
        setup_error=ObjectStoreRouteUnavailable("setup", "cleanup pending")
    )
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
        object_store=object_store,
    )

    result = await service.copy(
        src_session_id=str(source.session_id),
        src_path="source.bin",
        dst_session_id=str(destination.session_id),
        dst_path="destination.bin",
        kind="file",
        overwrite=True,
        chunk_size=4,
    )

    operations = [op for _executor_id, op in transport.calls]
    assert result.transport == "control_relay"
    assert result.fallbacks == ["object_store:setup"]
    assert bytes(transport.destination) == payload
    assert "transfer.url_upload" not in operations
    assert "transfer.http_upload" in operations
    assert "transfer.http_download" in operations


@pytest.mark.asyncio
async def test_cross_executor_object_store_setup_error_does_not_fallback(
    tmp_path,
):
    payload = b"setup-error"
    object_store = _ObjectStoreStub(
        setup_error=RuntimeError("cleanup registry is invalid")
    )
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
        object_store=object_store,
    )

    with pytest.raises(RuntimeError, match="cleanup registry is invalid"):
        await service.copy(
            src_session_id=str(source.session_id),
            src_path="source.bin",
            dst_session_id=str(destination.session_id),
            dst_path="destination.bin",
            kind="file",
            overwrite=True,
            chunk_size=4,
        )

    operations = [op for _executor_id, op in transport.calls]
    assert "transfer.url_upload" not in operations
    assert "transfer.http_upload" not in operations


@pytest.mark.asyncio
async def test_cross_executor_object_download_failure_aborts_before_control_fallback(
    tmp_path,
):
    payload = b"partial-object-download" * 100
    object_store = _ObjectStoreStub()
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
        object_store=object_store,
        object_download_failures=1,
    )

    result = await service.copy(
        src_session_id=str(source.session_id),
        src_path="source.bin",
        dst_session_id=str(destination.session_id),
        dst_path="destination.bin",
        kind="file",
        overwrite=True,
        chunk_size=64,
    )

    operations = [op for _executor_id, op in transport.calls]
    assert result.transport == "control_relay"
    assert len(result.fallbacks) == 1
    assert result.fallbacks == ["object_store:download"]
    assert "do-not-leak" not in repr(result.model_dump())
    assert operations.count("transfer.url_download") == 1
    assert "transfer_abandon_import" in operations
    assert operations.index("transfer_abandon_import") < operations.index(
        "transfer.http_download"
    )
    assert bytes(transport.destination) == payload


@pytest.mark.asyncio
async def test_cross_executor_object_destination_failure_does_not_fallback(
    tmp_path,
):
    payload = b"destination-semantic-error"
    object_store = _ObjectStoreStub()
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
        object_store=object_store,
        object_semantic_failure=True,
    )

    with pytest.raises(RuntimeError, match="operation_failed") as exc:
        await service.copy(
            src_session_id=str(source.session_id),
            src_path="source.bin",
            dst_session_id=str(destination.session_id),
            dst_path="destination.bin",
            kind="file",
            overwrite=True,
            chunk_size=8,
        )

    assert "do-not-leak" not in str(exc.value)
    operations = [op for _executor_id, op in transport.calls]
    assert "transfer.url_upload" in operations
    assert "transfer.url_download" in operations
    assert "transfer.http_upload" not in operations
    assert object_store.finished == 1


@pytest.mark.asyncio
async def test_cross_executor_object_cleanup_failure_is_visible_after_success(
    tmp_path,
):
    payload = b"cleanup-visible"
    object_store = _ObjectStoreStub(cleanup_error="OSError")
    service, source, destination, _transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
        object_store=object_store,
    )

    result = await service.copy(
        src_session_id=str(source.session_id),
        src_path="source.bin",
        dst_session_id=str(destination.session_id),
        dst_path="destination.bin",
        kind="file",
        overwrite=True,
    )

    assert result.transport == "object_store"
    assert "object-store cleanup failed (OSError)" in result.cleanup_errors


@pytest.mark.asyncio
async def test_cross_executor_payload_limit_rejects_before_raw_upload(tmp_path):
    payload = b"payload-too-large"
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
        max_payload_bytes=len(payload) - 1,
        max_store_bytes=10_000_000,
    )

    with pytest.raises(TransferPayloadCapacityError, match="per-transfer"):
        await service.copy(
            src_session_id=str(source.session_id),
            src_path="source.bin",
            dst_session_id=str(destination.session_id),
            dst_path="destination.bin",
            kind="file",
        )

    operations = [op for _executor_id, op in transport.calls]
    assert "transfer_stat" in operations
    assert "transfer.http_upload" not in operations
    assert "transfer.http_download" not in operations


@pytest.mark.asyncio
async def test_cross_executor_first_file_copy_uses_raw_http(tmp_path):
    payload = b"first-export-file" * 5
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
    )
    progress: list[dict[str, Any]] = []

    async def report(value: dict[str, Any]) -> None:
        progress.append(value)

    result = await service.copy(
        src_session_id=str(source.session_id),
        src_path="source.bin",
        dst_session_id=str(destination.session_id),
        dst_path="destination.bin",
        kind="auto",
        overwrite=True,
        chunk_size=7,
        progress=report,
    )

    assert result.kind == "file"
    assert result.transport == "control_relay"
    assert result.bytes == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert bytes(transport.destination) == payload
    assert progress[0]["phase"] == "stat"
    assert any(row["phase"] == "exporting" for row in progress)
    assert any(row["phase"] == "exported" for row in progress)
    assert any(row["phase"] == "importing" for row in progress)
    operations = [op for _executor_id, op in transport.calls]
    assert "transfer.http_upload" in operations
    assert "transfer.http_download" in operations
    assert "transfer_read_chunk" not in operations
    assert "transfer_write_chunk" not in operations
    assert (
        list(service._payloads.directory("transfer").glob("payload_*.bin"))
        == []
    )


@pytest.mark.asyncio
async def test_cross_executor_first_directory_copy_uses_raw_http(
    tmp_path,
):
    archive = b"opaque-archive-bytes" * 4
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="dir",
        payload=b"",
        archive=archive,
    )

    result = await service.copy(
        src_session_id=str(source.session_id),
        src_path="tree",
        dst_session_id=str(destination.session_id),
        dst_path="tree-copy",
        kind="dir",
        overwrite=False,
        chunk_size=5,
    )

    assert result.kind == "dir"
    assert result.transport == "control_relay"
    assert result.archive_bytes == len(archive)
    assert result.archive_sha256 == hashlib.sha256(archive).hexdigest()
    assert result.entries == 3
    assert bytes(transport.destination) == archive
    source_ops = [
        op
        for executor, op in transport.calls
        if executor == str(source.executor_id)
    ]
    assert "transfer_pack_dir" in source_ops
    assert "transfer.http_upload" in source_ops
    assert "transfer_read_chunk" not in source_ops
    assert "transfer_delete_temp_path" in source_ops
    destination_ops = [
        op
        for executor, op in transport.calls
        if executor == str(destination.executor_id)
    ]
    assert "transfer_alloc_temp_path" in destination_ops
    assert "transfer.http_download" in destination_ops
    assert "transfer_write_chunk" not in destination_ops
    assert "transfer_unpack_archive" in destination_ops
    assert "transfer_release_receipts" in destination_ops


class _ManagedContextProbe:
    def __init__(self) -> None:
        self.job_id = "job_" + "c" * 12
        self.logs: list[str] = []
        self.progress: list[dict[str, Any]] = []

    async def log(self, message: str) -> None:
        self.logs.append(message)

    async def update_progress(self, **progress: Any) -> None:
        self.progress.append(progress)


@pytest.mark.asyncio
async def test_managed_copy_handler_replays_durable_payload_and_reports_progress(
    monkeypatch,
):
    service = object.__new__(ControlSessionCopyService)
    context = _ManagedContextProbe()
    source_id = str(new_session_id())
    destination_id = str(new_session_id())
    source_executor = str(new_executor_id())
    destination_executor = str(new_executor_id())
    observed: dict[str, Any] = {}

    async def fake_copy(**kwargs: Any):
        observed.update(kwargs)
        report = kwargs["progress"]
        await report(
            {"phase": "stat", "bytes_transferred": 0, "total_bytes": 0}
        )
        await report(
            {"phase": "exporting", "bytes_transferred": 1, "total_bytes": 10}
        )
        await report(
            {"phase": "exporting", "bytes_transferred": 10, "total_bytes": 10}
        )
        return session_copy_module.SessionCopyOutput.model_validate(
            {
                "kind": "file",
                "transport": "control_relay",
                "resumed_bytes": 3,
                "source": {
                    "session_id": source_id,
                    "executor_id": source_executor,
                    "workdir": "src",
                    "path": "source.bin",
                    "resolved_path": "source.bin",
                },
                "destination": {
                    "session_id": destination_id,
                    "executor_id": destination_executor,
                    "workdir": "dst",
                    "path": "dest.bin",
                    "resolved_path": "dest.bin",
                },
                "relation": {
                    "route": "different_executors",
                    "same_session": False,
                    "same_executor": False,
                },
                "bytes": 10,
                "sha256": hashlib.sha256(b"0123456789").hexdigest(),
                "chunks": 2,
                "chunk_size": 8,
            }
        )

    monkeypatch.setattr(service, "copy", fake_copy)
    payload = {
        "transfer_id": "copy_" + "d" * 22,
        "src_session_id": source_id,
        "src_path": "source.bin",
        "dst_session_id": destination_id,
        "dst_path": "dest.bin",
        "kind": "file",
        "overwrite": True,
        "chunk_size": 8,
        "bindings": {
            source_id: {
                "executor_id": source_executor,
                "workdir": "src",
                "ignored": 7,
            },
            destination_id: {
                "executor_id": destination_executor,
                "workdir": "dst",
            },
            9: {"executor_id": "ignored"},
        },
    }

    result = await service._run_managed_job(context, payload)  # type: ignore[arg-type]

    assert observed["transfer_id"] == payload["transfer_id"]
    assert observed["owner_job_id"] == context.job_id
    assert observed["expected_bindings"] == {
        source_id: {"executor_id": source_executor, "workdir": "src"},
        destination_id: {"executor_id": destination_executor, "workdir": "dst"},
    }
    assert context.logs[0].startswith("copying ")
    assert context.logs[-1].startswith("copy completed:")
    assert context.progress[-1]["phase"] == "completed"
    assert context.progress[-1]["resumed_bytes"] == 3
    assert result["transport"] == "control_relay"


@pytest.mark.asyncio
async def test_cross_executor_directory_records_source_cleanup_failure(
    tmp_path,
):
    archive = b"archive" * 4
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="dir",
        payload=b"",
        archive=archive,
    )
    real_call = transport.call

    async def fail_cleanup(executor_id: str, op: str, args=None, **kwargs):
        if (
            executor_id == str(source.executor_id)
            and op == "transfer_delete_temp_path"
        ):
            raise RuntimeError("cleanup unavailable")
        return await real_call(executor_id, op, args, **kwargs)

    transport.call = fail_cleanup  # type: ignore[method-assign]
    result = await service.copy(
        src_session_id=str(source.session_id),
        src_path="tree",
        dst_session_id=str(destination.session_id),
        dst_path="tree-copy",
        kind="dir",
        chunk_size=4,
    )

    assert result.kind == "dir"
    assert any(
        "source archive cleanup failed" in error
        for error in result.cleanup_errors
    )


@pytest.mark.asyncio
async def test_cross_executor_export_rejects_changed_raw_upload(tmp_path):
    payload = b"source-bytes"
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
    )
    service._transfer_gateway.upload_bytes = payload + b"-changed"  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="raw upload integrity mismatch"):
        await service.copy(
            src_session_id=str(source.session_id),
            src_path="source.bin",
            dst_session_id=str(destination.session_id),
            dst_path="dest.bin",
            chunk_size=4,
        )

    assert transport.destination == b""
    operations = [op for _executor_id, op in transport.calls]
    assert "transfer.http_upload" in operations
    assert "transfer_read_chunk" not in operations
    assert "transfer_write_chunk" not in operations


@pytest.mark.asyncio
async def test_cross_executor_import_recovers_lost_finish_ack(tmp_path):
    payload = b"recover-finish"
    service, _sessions, transport, checkpoint, _state_store = (
        _checkpoint_service(tmp_path, payload)
    )
    real_call = transport.call
    finish_failed = False

    async def lost_finish(executor_id: str, op: str, args=None, **kwargs):
        nonlocal finish_failed
        if op == "transfer_finish_write" and not finish_failed:
            finish_failed = True
            await real_call(executor_id, op, args, **kwargs)
            raise RuntimeError("lost finish response")
        if op == "transfer_begin_write" and finish_failed:
            values = args or {}
            return ExecutorResult(
                id=new_command_id(),
                ok=True,
                result={
                    "path": str(values["path"]),
                    "temp_path": ".temporary",
                    "transfer_id": str(values["transfer_id"]),
                    "created": True,
                    "expected_bytes": len(payload),
                    "offset": len(payload),
                    "resumed": True,
                    "completed": True,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            )
        return await real_call(executor_id, op, args, **kwargs)

    transport.call = lost_finish  # type: ignore[method-assign]
    result = await service.copy(
        src_session_id=str(checkpoint.source_session_id),
        src_path=checkpoint.source_path,
        dst_session_id=str(checkpoint.destination_session_id),
        dst_path=checkpoint.destination_path,
        kind="file",
        chunk_size=checkpoint.chunk_size,
        transfer_id=checkpoint.transfer_id,
    )

    assert finish_failed is True
    assert result.bytes == len(payload)
    assert result.resumed_bytes == 0


@pytest.mark.asyncio
async def test_cross_executor_imported_checkpoint_returns_without_executor_contact(
    tmp_path,
):
    payload = b"already-imported"
    service, sessions, transport, checkpoint, _state_store = (
        _checkpoint_service(tmp_path, payload)
    )
    checkpoint = service._checkpoints.update(
        checkpoint.transfer_id,
        import_path=checkpoint.destination_path,
        import_resource_id=checkpoint.transfer_id,
        destination_resolved_path=checkpoint.destination_path,
        chunks=1,
        last_known_step="imported",
    )

    result = await service.copy(
        src_session_id=str(checkpoint.source_session_id),
        src_path=checkpoint.source_path,
        dst_session_id=str(checkpoint.destination_session_id),
        dst_path=checkpoint.destination_path,
        kind="file",
        chunk_size=checkpoint.chunk_size,
        transfer_id=checkpoint.transfer_id,
    )

    assert sessions.require_available == ()
    assert transport.calls == []
    assert result.bytes == len(payload)
    assert result.resumed_bytes == 0


@pytest.mark.asyncio
async def test_imported_checkpoint_releases_receipts_when_destination_is_available(
    tmp_path,
):
    payload = b"already-imported-online"
    service, sessions, transport, checkpoint, _state_store = (
        _checkpoint_service(tmp_path, payload)
    )
    checkpoint = service._checkpoints.update(
        checkpoint.transfer_id,
        import_path=checkpoint.destination_path,
        import_resource_id=checkpoint.transfer_id,
        destination_resolved_path=checkpoint.destination_path,
        chunks=1,
        last_known_step="imported",
    )
    sessions.availability = "available"

    result = await service.copy(
        src_session_id=str(checkpoint.source_session_id),
        src_path=checkpoint.source_path,
        dst_session_id=str(checkpoint.destination_session_id),
        dst_path=checkpoint.destination_path,
        kind="file",
        chunk_size=checkpoint.chunk_size,
        transfer_id=checkpoint.transfer_id,
    )

    assert result.bytes == len(payload)
    assert transport.calls == ["transfer_release_receipts"]


def test_managed_retry_availability_follows_feature_checkpoint(tmp_path):
    owner_job_id = "job_" + "e" * 12
    service, _sessions, _transport, checkpoint, _state_store = (
        _checkpoint_service(
            tmp_path,
            b"retry-availability",
            owner_job_id=owner_job_id,
        )
    )
    references = (
        str(checkpoint.source_session_id),
        str(checkpoint.destination_session_id),
    )

    assert service.retry_require_available(owner_job_id, references) == (
        str(checkpoint.destination_session_id),
    )
    assert (
        service.retry_require_available("job_" + "f" * 12, references)
        == references
    )

    service._checkpoints.update(
        checkpoint.transfer_id,
        import_path=checkpoint.destination_path,
        import_resource_id=checkpoint.transfer_id,
        destination_resolved_path=checkpoint.destination_path,
        last_known_step="imported",
    )
    assert service.retry_require_available(owner_job_id, references) == ()

    with pytest.raises(
        RuntimeError, match="does not match managed job references"
    ):
        service.retry_require_available(
            owner_job_id,
            (str(checkpoint.source_session_id),),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [None, False, True])
async def test_public_session_copy_router_defaults_are_safe_and_tracked(
    background: bool | None,
) -> None:
    class CopyService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def copy(self, **kwargs):
            self.calls.append(("copy", kwargs))
            return {"ok": True}

        async def start_background(self, **kwargs):
            self.calls.append(("background", kwargs))
            return {"ok": True}

    copy_service = CopyService()
    router = ControlToolRouter(
        sessions=object(),  # type: ignore[arg-type]
        session_copy=copy_service,  # type: ignore[arg-type]
        jobs=object(),  # type: ignore[arg-type]
        downloads=object(),  # type: ignore[arg-type]
        executors=object(),  # type: ignore[arg-type]
        tasks=object(),  # type: ignore[arg-type]
        audit=object(),  # type: ignore[arg-type]
        agent_bridge=object(),  # type: ignore[arg-type]
    )

    args: dict[str, Any] = {
        "src_session_id": "session_src",
        "src_path": "source",
        "dst_session_id": "session_dst",
        "dst_path": "destination",
    }
    if background is not None:
        args["background"] = background
    result = await router.invoke("session_copy", args)

    assert result == {"ok": True}
    assert copy_service.calls == [
        (
            "copy" if background is False else "background",
            {
                "src_session_id": "session_src",
                "src_path": "source",
                "dst_session_id": "session_dst",
                "dst_path": "destination",
                "kind": "auto",
                "overwrite": False,
                "chunk_size": None,
            },
        )
    ]
