from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import workgate.control.session_copy as session_copy_module
from tests.helpers import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.session_copy import (
    SESSION_COPY_MANAGED_KIND,
    ControlSessionCopyService,
)
from workgate.control.state import ControlSessionRecord
from workgate.persistence import FileStateStore
from workgate.protocol.executor import ExecutorResult
from workgate.protocol.ids import (
    new_command_id,
    new_executor_id,
    new_session_id,
)
from workgate.schemas.result_models.jobs import JobStartOutput


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
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


@pytest.mark.asyncio
async def test_shared_session_copy_streams_file_on_same_executor(
    tmp_path, monkeypatch
):
    payload = b"abcdef" * 1000
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "payload.bin").write_bytes(payload)
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)
    progress: list[dict[str, Any]] = []

    async def report(value: dict[str, Any]) -> None:
        progress.append(value)

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

    with pytest.raises(RuntimeError, match="transfer_begin_write failed"):
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


class _DestinationOnlyTransport:
    def __init__(
        self, source_executor_id: str, destination_executor_id: str
    ) -> None:
        self.source_executor_id = source_executor_id
        self.destination_executor_id = destination_executor_id
        self.calls: list[str] = []
        self.data = bytearray()
        self.fail_release_receipts = False
        self.fail_abandon_import = False

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
        elif op == "transfer_write_chunk":
            raw = base64.b64decode(str(values["data_b64"]))
            assert int(values["offset"]) == len(self.data)
            assert hashlib.sha256(raw).hexdigest() == values["expected_sha256"]
            self.data.extend(raw)
            result = {
                "path": str(values["path"]),
                "temp_path": ".temporary",
                "offset": int(values["offset"]),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
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
            result = {
                "write_reconciled": True,
                "unpack_reconciled": False,
            }
        else:
            raise AssertionError(f"unexpected destination operation: {op}")
        return ExecutorResult(id=new_command_id(), ok=True, result=result)


def _checkpoint_service(
    tmp_path: Path, payload: bytes, *, owner_job_id: str | None = None
):
    source_executor_id = str(new_executor_id())
    destination_executor_id = str(new_executor_id())
    source_session_id = str(new_session_id())
    destination_session_id = str(new_session_id())
    source = ControlSessionRecord(
        session_id=source_session_id,
        executor_id=source_executor_id,
        requested_workdir="src",
        resolved_workdir_display="src",
        status="active",
        created_at=1.0,
        updated_at=1.0,
    )
    destination = ControlSessionRecord(
        session_id=destination_session_id,
        executor_id=destination_executor_id,
        requested_workdir="dst",
        resolved_workdir_display="dst",
        status="active",
        created_at=1.0,
        updated_at=1.0,
    )
    sessions = _CheckpointSessions((source, destination))
    transport = _DestinationOnlyTransport(
        source_executor_id, destination_executor_id
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
    )
    transfer_id = "copy_" + "a" * 22
    staging = service._payloads.new_staging_path("transfer")
    with service._payloads.open_private_staging(
        staging, namespace="transfer"
    ) as handle:
        handle.write(payload)
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
async def test_cross_executor_retry_uses_control_payload_with_source_offline(
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
    assert result.transport == "control_payload"
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
async def test_corrupt_control_payload_fails_before_destination_side_effect(
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

    assert transport.calls == []


class _FirstExportTransport:
    def __init__(
        self,
        source_executor_id: str,
        destination_executor_id: str,
        *,
        source_kind: str,
        payload: bytes,
        archive: bytes | None = None,
    ) -> None:
        self.source_executor_id = source_executor_id
        self.destination_executor_id = destination_executor_id
        self.source_kind = source_kind
        self.payload = payload
        self.archive = archive
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
            elif op == "transfer_read_chunk":
                source = (
                    self.payload if self.source_kind == "file" else self.archive
                )
                assert source is not None
                offset = int(values["offset"])
                chunk_size = int(values["chunk_size"])
                raw = source[offset : offset + chunk_size]
                result = {
                    "path": str(values["path"]),
                    "offset": offset,
                    "bytes": len(raw),
                    "size": len(source),
                    "eof": offset + len(raw) >= len(source),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "data_b64": base64.b64encode(raw).decode("ascii"),
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
                    "offset": 0,
                    "resumed": False,
                    "completed": False,
                    "sha256": None,
                }
            elif op == "transfer_write_chunk":
                raw = base64.b64decode(str(values["data_b64"]))
                assert int(values["offset"]) == len(self.destination)
                assert (
                    hashlib.sha256(raw).hexdigest() == values["expected_sha256"]
                )
                self.destination.extend(raw)
                result = {
                    "path": str(values["path"]),
                    "temp_path": ".temporary",
                    "offset": int(values["offset"]),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
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
):
    source_executor_id = str(new_executor_id())
    destination_executor_id = str(new_executor_id())
    source = ControlSessionRecord(
        session_id=str(new_session_id()),
        executor_id=source_executor_id,
        requested_workdir="src",
        resolved_workdir_display="src",
        status="active",
        created_at=1.0,
        updated_at=1.0,
    )
    destination = ControlSessionRecord(
        session_id=str(new_session_id()),
        executor_id=destination_executor_id,
        requested_workdir="dst",
        resolved_workdir_display="dst",
        status="active",
        created_at=1.0,
        updated_at=1.0,
    )
    sessions = _CheckpointSessions((source, destination))
    transport = _FirstExportTransport(
        source_executor_id,
        destination_executor_id,
        source_kind=source_kind,
        payload=payload,
        archive=archive,
    )
    state_store = FileStateStore(lambda: tmp_path / "state")
    service = ControlSessionCopyService(
        sessions,  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        state_store,
        tmp_path / "data",
    )
    return service, source, destination, transport


@pytest.mark.asyncio
async def test_cross_executor_first_file_copy_exports_control_payload(tmp_path):
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
    assert result.transport == "control_payload"
    assert result.bytes == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert bytes(transport.destination) == payload
    assert progress[0]["phase"] == "stat"
    assert any(row["phase"] == "exporting" for row in progress)
    assert any(row["phase"] == "exported" for row in progress)
    assert any(row["phase"] == "importing" for row in progress)
    assert (
        list(service._payloads.directory("transfer").glob("payload_*.bin"))
        == []
    )


@pytest.mark.asyncio
async def test_cross_executor_first_directory_copy_exports_archive_payload(
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
    assert result.transport == "control_payload"
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
    assert "transfer_delete_temp_path" in source_ops
    destination_ops = [
        op
        for executor, op in transport.calls
        if executor == str(destination.executor_id)
    ]
    assert "transfer_alloc_temp_path" in destination_ops
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
                "transport": "control_payload",
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
    assert result["transport"] == "control_payload"


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
async def test_cross_executor_export_rejects_changed_source_chunk(tmp_path):
    payload = b"source-bytes"
    service, source, destination, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind="file",
        payload=payload,
    )
    real_call = transport.call

    async def changed_chunk(executor_id: str, op: str, args=None, **kwargs):
        result = await real_call(executor_id, op, args, **kwargs)
        if (
            executor_id == str(source.executor_id)
            and op == "transfer_read_chunk"
        ):
            result.result["offset"] = int(result.result["offset"]) + 1  # type: ignore[index]
        return result

    transport.call = changed_chunk  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="source changed"):
        await service.copy(
            src_session_id=str(source.session_id),
            src_path="source.bin",
            dst_session_id=str(destination.session_id),
            dst_path="dest.bin",
            chunk_size=4,
        )
    assert transport.destination == b""


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
