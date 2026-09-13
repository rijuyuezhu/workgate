"""Control-orchestrated copy between two existing final shared sessions."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePath
from typing import Any, Literal, cast

from pydantic import JsonValue

from ..jobs.managed import (
    ManagedJobContext,
    ManagedJobHandler,
    start_managed_job_without_session_admission,
)
from ..persistence import StateStore
from ..protocol.transfer import normalize_chunk_size
from ..schemas.result_models.jobs import JobStartOutput
from ..schemas.result_models.session import (
    SessionCopyEndpoint,
    SessionCopyOutput,
    SessionCopyRelation,
)
from ..schemas.result_models.transfer import (
    TransferAllocTempPathOutput,
    TransferBeginWriteOutput,
    TransferFinishWriteOutput,
    TransferPackDirOutput,
    TransferReadChunkOutput,
    TransferStatOutput,
    TransferUnpackArchiveOutput,
)
from .executor_transport import ExecutorTransport
from .payload_store import PayloadStore
from .session_copy_store import (
    SessionCopyCheckpoint,
    SessionCopyCheckpointStore,
)
from .sessions import ControlSessionCoordinator
from .state import ControlSessionRecord

CopyKind = Literal["auto", "file", "dir"]
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
SESSION_COPY_MANAGED_KIND = "session-copy"


class ControlSessionCopyService:
    """Copy data without creating, migrating, or rebinding either session."""

    def __init__(
        self,
        sessions: ControlSessionCoordinator,
        transport: ExecutorTransport,
        state_store: StateStore,
        data_dir: Path,
    ) -> None:
        self._sessions = sessions
        self._transport = transport
        self._payloads = PayloadStore(data_dir)
        self._checkpoints = SessionCopyCheckpointStore(
            state_store, self._payloads
        )

    async def copy(
        self,
        *,
        src_session_id: str,
        src_path: str,
        dst_session_id: str,
        dst_path: str,
        kind: CopyKind = "auto",
        overwrite: bool = True,
        chunk_size: int | None = None,
        progress: ProgressCallback | None = None,
        expected_bindings: dict[str, dict[str, str]] | None = None,
        transfer_id: str | None = None,
        owner_job_id: str | None = None,
    ) -> SessionCopyOutput:
        chunk_bytes = normalize_chunk_size(chunk_size)
        self._checkpoints.prune()
        active_transfer_id = transfer_id or (
            "copy_" + secrets.token_urlsafe(16)
        )
        checkpoint = self._checkpoints.load(active_transfer_id)
        if checkpoint is None:
            require_available = (src_session_id, dst_session_id)
        elif checkpoint.last_known_step == "imported":
            require_available = ()
        else:
            require_available = (dst_session_id,)
        async with self._sessions.session_admission(
            (src_session_id, dst_session_id),
            require_available=require_available,
        ) as records:
            by_id = {str(record.session_id): record for record in records}
            src = by_id[src_session_id]
            dst = by_id[dst_session_id]
            if expected_bindings is not None:
                self._validate_expected_binding(
                    src, expected_bindings.get(src_session_id), label="source"
                )
                self._validate_expected_binding(
                    dst,
                    expected_bindings.get(dst_session_id),
                    label="destination",
                )
            if checkpoint is not None:
                self._validate_checkpoint(
                    checkpoint,
                    src=src,
                    src_path=src_path,
                    dst=dst,
                    dst_path=dst_path,
                    kind=kind,
                    overwrite=overwrite,
                    chunk_size=chunk_bytes,
                    owner_job_id=owner_job_id,
                )
            if src.executor_id != dst.executor_id:
                try:
                    resolved_kind, metrics = await self._copy_cross_executor(
                        src,
                        src_path=src_path,
                        dst=dst,
                        dst_path=dst_path,
                        kind=kind,
                        overwrite=overwrite,
                        chunk_size=chunk_bytes,
                        progress=progress,
                        transfer_id=active_transfer_id,
                        owner_job_id=owner_job_id,
                        checkpoint=checkpoint,
                    )
                    return self._output(
                        src,
                        src_path,
                        dst,
                        dst_path,
                        resolved_kind,
                        metrics,
                    )
                finally:
                    if owner_job_id is None:
                        self._checkpoints.remove(active_transfer_id)
            await self._report(
                progress,
                phase="stat",
                bytes_transferred=0,
                total_bytes=0,
            )
            stat = TransferStatOutput.model_validate(
                await self._call(
                    src,
                    "transfer_stat",
                    {"path": src_path, "sha256": kind != "dir"},
                )
            )
            resolved_kind = self._resolve_kind(kind, stat)
            if resolved_kind == "file":
                metrics = await self._copy_file(
                    src,
                    stat,
                    src_path=src_path,
                    dst=dst,
                    dst_path=dst_path,
                    overwrite=overwrite,
                    chunk_size=chunk_bytes,
                    progress=progress,
                )
            else:
                metrics = await self._copy_dir(
                    src,
                    src_path=src_path,
                    dst=dst,
                    dst_path=dst_path,
                    overwrite=overwrite,
                    chunk_size=chunk_bytes,
                    progress=progress,
                )
            return self._output(
                src,
                src_path,
                dst,
                dst_path,
                resolved_kind,
                metrics,
            )

    async def start_background(
        self,
        *,
        src_session_id: str,
        src_path: str,
        dst_session_id: str,
        dst_path: str,
        kind: CopyKind = "auto",
        overwrite: bool = True,
        chunk_size: int | None = None,
    ) -> JobStartOutput:
        """Start one durable control-managed copy under shared-session admission."""
        normalized_chunk_size = normalize_chunk_size(chunk_size)
        async with self._sessions.session_admission(
            (src_session_id, dst_session_id)
        ) as records:
            by_id = {str(record.session_id): record for record in records}
            payload = {
                "transfer_id": "copy_" + secrets.token_urlsafe(16),
                "src_session_id": src_session_id,
                "src_path": src_path,
                "dst_session_id": dst_session_id,
                "dst_path": dst_path,
                "kind": kind,
                "overwrite": overwrite,
                "chunk_size": normalized_chunk_size,
                "bindings": {
                    src_session_id: self._binding_snapshot(
                        by_id[src_session_id]
                    ),
                    dst_session_id: self._binding_snapshot(
                        by_id[dst_session_id]
                    ),
                },
            }
            destination_name = PurePath(dst_path).name or "artifact"
            return await start_managed_job_without_session_admission(
                src_session_id,
                SESSION_COPY_MANAGED_KIND,
                payload,
                name=f"copy-{destination_name}"[:80],
                command=(
                    f"session_copy {src_session_id}:{src_path} -> "
                    f"{dst_session_id}:{dst_path}"
                ),
                cwd=".",
            )

    def managed_job_registration(self) -> tuple[str, ManagedJobHandler]:
        """Return this runtime's shared-session-aware managed-copy handler."""
        return SESSION_COPY_MANAGED_KIND, self._run_managed_job

    def retry_require_available(
        self, job_id: str, session_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Return executor availability required by one managed-copy retry."""
        checkpoint = self._checkpoints.load_for_owner_job(job_id)
        if checkpoint is None:
            return session_ids
        expected = {
            str(checkpoint.source_session_id),
            str(checkpoint.destination_session_id),
        }
        if not expected.issubset(set(session_ids)):
            raise RuntimeError(
                "session-copy checkpoint does not match managed job references"
            )
        if checkpoint.last_known_step == "imported":
            return ()
        return (str(checkpoint.destination_session_id),)

    async def _run_managed_job(
        self, context: ManagedJobContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        last_report_at = 0.0
        last_phase = ""

        async def report(progress: dict[str, Any]) -> None:
            nonlocal last_phase, last_report_at
            now = time.monotonic()
            phase = str(progress.get("phase") or "")
            transferred = int(progress.get("bytes_transferred") or 0)
            total = int(progress.get("total_bytes") or 0)
            if (
                phase != last_phase
                or transferred == 0
                or (total > 0 and transferred >= total)
                or now - last_report_at >= 0.5
            ):
                await context.update_progress(**progress)
                last_phase = phase
                last_report_at = now

        src_session_id = str(payload["src_session_id"])
        transfer_id = str(payload["transfer_id"])
        src_path = str(payload["src_path"])
        dst_session_id = str(payload["dst_session_id"])
        dst_path = str(payload["dst_path"])
        kind = cast(CopyKind, str(payload.get("kind") or "auto"))
        overwrite = bool(payload.get("overwrite", True))
        raw_chunk_size = payload.get("chunk_size")
        chunk_size = int(raw_chunk_size) if raw_chunk_size is not None else None
        raw_bindings = payload.get("bindings")
        expected_bindings = (
            {
                str(session_id): {
                    str(key): str(value)
                    for key, value in binding.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                for session_id, binding in raw_bindings.items()
                if isinstance(session_id, str) and isinstance(binding, dict)
            }
            if isinstance(raw_bindings, dict)
            else None
        )
        await context.log(
            f"copying {src_session_id}:{src_path} -> {dst_session_id}:{dst_path}"
        )
        result = await self.copy(
            src_session_id=src_session_id,
            src_path=src_path,
            dst_session_id=dst_session_id,
            dst_path=dst_path,
            kind=kind,
            overwrite=overwrite,
            chunk_size=chunk_size,
            progress=report,
            expected_bindings=expected_bindings,
            transfer_id=transfer_id,
            owner_job_id=context.job_id,
        )
        total_bytes = int(result.bytes or result.archive_bytes or 0)
        await context.update_progress(
            phase="completed",
            bytes_transferred=total_bytes,
            total_bytes=total_bytes,
            chunks=result.chunks,
            chunk_size=result.chunk_size,
            kind=result.kind,
            route=result.relation.route,
            transport=result.transport,
            resumed_bytes=result.resumed_bytes,
        )
        await context.log(
            f"copy completed: {result.kind}, {total_bytes} bytes, {result.chunks} chunks"
        )
        return result.model_dump(mode="json")

    async def _copy_cross_executor(
        self,
        src: ControlSessionRecord,
        *,
        src_path: str,
        dst: ControlSessionRecord,
        dst_path: str,
        kind: CopyKind,
        overwrite: bool,
        chunk_size: int,
        progress: ProgressCallback | None,
        transfer_id: str,
        owner_job_id: str | None,
        checkpoint: SessionCopyCheckpoint | None,
    ) -> tuple[Literal["file", "dir"], dict[str, Any]]:
        current = checkpoint
        if current is None:
            await self._report(
                progress,
                phase="stat",
                bytes_transferred=0,
                total_bytes=0,
            )
            stat = TransferStatOutput.model_validate(
                await self._call(
                    src,
                    "transfer_stat",
                    {"path": src_path, "sha256": kind != "dir"},
                )
            )
            resolved_kind = self._resolve_kind(kind, stat)
            cleanup_errors: list[str] = []
            if resolved_kind == "file":
                if stat.size is None or stat.sha256 is None:
                    raise RuntimeError(
                        "source file stat is missing size or sha256"
                    )
                current = await self._export_payload(
                    src,
                    export_path=src_path,
                    source_resolved_path=stat.path,
                    payload_size=stat.size,
                    payload_sha256=stat.sha256,
                    source_unbound_temp=False,
                    src_path=src_path,
                    dst=dst,
                    dst_path=dst_path,
                    kind="file",
                    overwrite=overwrite,
                    chunk_size=chunk_size,
                    transfer_id=transfer_id,
                    owner_job_id=owner_job_id,
                    progress=progress,
                    cleanup_errors=cleanup_errors,
                )
            else:
                await self._report(
                    progress,
                    phase="packing",
                    bytes_transferred=0,
                    total_bytes=0,
                )
                pack = TransferPackDirOutput.model_validate(
                    await self._call(
                        src,
                        "transfer_pack_dir",
                        {"path": src_path, "compression": "gz"},
                    )
                )
                try:
                    current = await self._export_payload(
                        src,
                        export_path=pack.archive_path,
                        source_resolved_path=pack.path,
                        payload_size=pack.bytes,
                        payload_sha256=pack.sha256,
                        source_unbound_temp=True,
                        src_path=src_path,
                        dst=dst,
                        dst_path=dst_path,
                        kind="dir",
                        overwrite=overwrite,
                        chunk_size=chunk_size,
                        transfer_id=transfer_id,
                        owner_job_id=owner_job_id,
                        progress=progress,
                        cleanup_errors=cleanup_errors,
                    )
                finally:
                    try:
                        await self._call(
                            src,
                            "transfer_delete_temp_path",
                            {"path": pack.archive_path},
                        )
                    except Exception as exc:
                        cleanup_errors.append(
                            f"source archive cleanup failed: {exc}"
                        )
                        if current is not None:
                            current = self._checkpoints.update(
                                transfer_id,
                                cleanup_errors=cleanup_errors,
                            )
        assert current is not None
        metrics = await self._import_checkpoint(
            current,
            dst=dst,
            progress=progress,
        )
        return current.kind, metrics

    async def _export_payload(
        self,
        src: ControlSessionRecord,
        *,
        export_path: str,
        source_resolved_path: str,
        payload_size: int,
        payload_sha256: str,
        source_unbound_temp: bool,
        src_path: str,
        dst: ControlSessionRecord,
        dst_path: str,
        kind: Literal["file", "dir"],
        overwrite: bool,
        chunk_size: int,
        transfer_id: str,
        owner_job_id: str | None,
        progress: ProgressCallback | None,
        cleanup_errors: list[str],
    ) -> SessionCopyCheckpoint:
        staging = self._payloads.new_staging_path("transfer")
        digest = hashlib.sha256()
        offset = 0
        try:
            with self._payloads.open_private_staging(
                staging, namespace="transfer"
            ) as handle:
                while offset < payload_size:
                    args: dict[str, JsonValue] = {
                        "path": export_path,
                        "offset": offset,
                        "chunk_size": chunk_size,
                    }
                    if source_unbound_temp:
                        args["_workgate_unbound_temp"] = True
                    chunk = TransferReadChunkOutput.model_validate(
                        await self._call(src, "transfer_read_chunk", args)
                    )
                    if (
                        chunk.offset != offset
                        or chunk.size != payload_size
                        or chunk.bytes <= 0
                    ):
                        raise RuntimeError(
                            "source changed during session_copy export"
                        )
                    raw = base64.b64decode(
                        chunk.data_b64.encode("ascii"), validate=True
                    )
                    if (
                        len(raw) != chunk.bytes
                        or hashlib.sha256(raw).hexdigest() != chunk.sha256
                    ):
                        raise RuntimeError(
                            "executor transfer chunk size mismatch"
                        )
                    handle.write(raw)
                    digest.update(raw)
                    offset += len(raw)
                    await self._report(
                        progress,
                        phase="exporting",
                        bytes_transferred=offset,
                        total_bytes=payload_size,
                        chunk_size=chunk_size,
                    )
            if offset != payload_size or digest.hexdigest() != payload_sha256:
                raise RuntimeError("control payload export integrity mismatch")
            checkpoint = self._checkpoints.commit_export(
                checkpoint={
                    "transfer_id": transfer_id,
                    "owner_job_id": owner_job_id,
                    "source_session_id": str(src.session_id),
                    "source_executor_id": str(src.executor_id),
                    "destination_session_id": str(dst.session_id),
                    "destination_executor_id": str(dst.executor_id),
                    "source_path": src_path,
                    "destination_path": dst_path,
                    "kind": kind,
                    "overwrite": overwrite,
                    "chunk_size": chunk_size,
                    "source_resolved_path": source_resolved_path,
                    "cleanup_errors": list(cleanup_errors),
                    "last_known_step": "exported",
                },
                staging_path=staging,
                payload_size=payload_size,
                payload_sha256=payload_sha256,
            )
            await self._report(
                progress,
                phase="exported",
                bytes_transferred=payload_size,
                total_bytes=payload_size,
                chunk_size=chunk_size,
            )
            return checkpoint
        finally:
            with contextlib.suppress(OSError):
                staging.unlink(missing_ok=True)

    async def _import_checkpoint(
        self,
        checkpoint: SessionCopyCheckpoint,
        *,
        dst: ControlSessionRecord,
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        current = checkpoint
        if current.last_known_step == "imported":
            if (
                await self._sessions.session_availability(str(dst.session_id))
                == "available"
            ):
                current = await self._release_import_receipts(current, dst=dst)
            return self._checkpoint_metrics(current)

        import_path = current.import_path
        if import_path is None:
            if current.kind == "file":
                import_path = current.destination_path
            else:
                allocated = TransferAllocTempPathOutput.model_validate(
                    await self._call(
                        dst,
                        "transfer_alloc_temp_path",
                        {"suffix": ".tar.gz"},
                    )
                )
                import_path = allocated.path
            current = self._checkpoints.update(
                current.transfer_id,
                import_path=import_path,
                import_resource_id=current.transfer_id,
                last_known_step="importing",
            )
        elif current.import_resource_id is None:
            current = self._checkpoints.update(
                current.transfer_id,
                import_resource_id=current.transfer_id,
                last_known_step="importing",
            )

        write = await self._write_payload_to_executor(
            current,
            dst=dst,
            import_path=import_path,
            unbound_temp=current.kind == "dir",
            progress=progress,
        )
        total_chunks = (
            0
            if current.payload_size == 0
            else (current.payload_size + current.chunk_size - 1)
            // current.chunk_size
        )
        if current.kind == "file":
            current = self._checkpoints.update(
                current.transfer_id,
                destination_resolved_path=write["path"],
                chunks=total_chunks,
                resumed_bytes=write["resumed_bytes"],
                last_known_step="imported",
            )
            current = await self._release_import_receipts(current, dst=dst)
            return self._checkpoint_metrics(current)

        unpack_args: dict[str, JsonValue] = {
            "archive_path": import_path,
            "dst_path": current.destination_path,
            "overwrite": current.overwrite,
            "cleanup_archive": True,
            "transfer_id": current.transfer_id,
        }
        try:
            unpack_raw = await self._call(
                dst, "transfer_unpack_archive", unpack_args
            )
        except Exception:
            # A lost acknowledgement after atomic directory publish is reconciled
            # by the executor's transfer-specific unpack receipt.
            unpack_raw = await self._call(
                dst, "transfer_unpack_archive", unpack_args
            )
        unpack = TransferUnpackArchiveOutput.model_validate(unpack_raw)
        cleanup_errors = [
            *current.cleanup_errors,
            *unpack.cleanup_errors,
        ]
        current = self._checkpoints.update(
            current.transfer_id,
            destination_resolved_path=unpack.path,
            entries=unpack.entries,
            chunks=total_chunks,
            resumed_bytes=write["resumed_bytes"],
            cleanup_errors=cleanup_errors,
            last_known_step="imported",
        )
        current = await self._release_import_receipts(current, dst=dst)
        return self._checkpoint_metrics(current)

    async def _release_import_receipts(
        self,
        checkpoint: SessionCopyCheckpoint,
        *,
        dst: ControlSessionRecord,
    ) -> SessionCopyCheckpoint:
        try:
            await self._call(
                dst,
                "transfer_release_receipts",
                {"transfer_id": checkpoint.transfer_id},
            )
        except Exception as exc:
            cleanup_errors = [
                *checkpoint.cleanup_errors,
                f"destination transfer receipt cleanup failed: {exc}",
            ]
            with contextlib.suppress(Exception):
                checkpoint = self._checkpoints.update(
                    checkpoint.transfer_id, cleanup_errors=cleanup_errors
                )
        return checkpoint

    async def _write_payload_to_executor(
        self,
        checkpoint: SessionCopyCheckpoint,
        *,
        dst: ControlSessionRecord,
        import_path: str,
        unbound_temp: bool,
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        handle, _payload_path = self._payloads.open_payload(
            checkpoint.payload_id,
            namespace="transfer",
            size=checkpoint.payload_size,
            sha256=checkpoint.payload_sha256,
        )
        begin_args: dict[str, JsonValue] = {
            "path": import_path,
            "overwrite": True if unbound_temp else checkpoint.overwrite,
            "expected_bytes": checkpoint.payload_size,
            "transfer_id": checkpoint.import_resource_id
            or checkpoint.transfer_id,
        }
        if unbound_temp:
            begin_args["_workgate_unbound_temp"] = True
        try:
            begin = TransferBeginWriteOutput.model_validate(
                await self._call(dst, "transfer_begin_write", begin_args)
            )
            if begin.offset < 0 or begin.offset > checkpoint.payload_size:
                raise RuntimeError(
                    "destination transfer resume offset is invalid"
                )
            resumed_bytes = begin.offset
            if begin.completed:
                if (
                    begin.offset != checkpoint.payload_size
                    or begin.sha256 != checkpoint.payload_sha256
                ):
                    raise RuntimeError(
                        "destination transfer receipt integrity mismatch"
                    )
                return {"path": begin.path, "resumed_bytes": resumed_bytes}

            offset = begin.offset
            handle.seek(offset)
            while offset < checkpoint.payload_size:
                raw = handle.read(
                    min(checkpoint.chunk_size, checkpoint.payload_size - offset)
                )
                if not raw:
                    raise RuntimeError(
                        "control payload made no forward progress"
                    )
                write_args: dict[str, JsonValue] = {
                    "path": import_path,
                    "transfer_id": begin.transfer_id,
                    "offset": offset,
                    "data_b64": base64.b64encode(raw).decode("ascii"),
                    "expected_sha256": hashlib.sha256(raw).hexdigest(),
                }
                if unbound_temp:
                    write_args["_workgate_unbound_temp"] = True
                await self._call(dst, "transfer_write_chunk", write_args)
                offset += len(raw)
                await self._report(
                    progress,
                    phase="importing",
                    bytes_transferred=offset,
                    total_bytes=checkpoint.payload_size,
                    chunk_size=checkpoint.chunk_size,
                    resumed_bytes=resumed_bytes,
                )
        finally:
            handle.close()

        finish_args: dict[str, JsonValue] = {
            "path": import_path,
            "transfer_id": begin.transfer_id,
            "expected_bytes": checkpoint.payload_size,
            "expected_sha256": checkpoint.payload_sha256,
        }
        if unbound_temp:
            finish_args["_workgate_unbound_temp"] = True
        try:
            finished_raw = await self._call(
                dst, "transfer_finish_write", finish_args
            )
            finished = TransferFinishWriteOutput.model_validate(finished_raw)
        except Exception as original:
            try:
                recovered = TransferBeginWriteOutput.model_validate(
                    await self._call(dst, "transfer_begin_write", begin_args)
                )
            except Exception:
                raise original from None
            if (
                not recovered.completed
                or recovered.offset != checkpoint.payload_size
                or recovered.sha256 != checkpoint.payload_sha256
            ):
                raise original from None
            return {"path": recovered.path, "resumed_bytes": resumed_bytes}
        if (
            not finished.completed
            or finished.bytes != checkpoint.payload_size
            or finished.sha256 != checkpoint.payload_sha256
        ):
            raise RuntimeError("destination transfer did not commit completely")
        return {"path": finished.path, "resumed_bytes": resumed_bytes}

    @staticmethod
    def _checkpoint_metrics(
        checkpoint: SessionCopyCheckpoint,
    ) -> dict[str, Any]:
        base = {
            "chunks": checkpoint.chunks,
            "chunk_size": checkpoint.chunk_size,
            "source_path": checkpoint.source_resolved_path,
            "destination_path": checkpoint.destination_resolved_path,
            "cleanup_errors": list(checkpoint.cleanup_errors),
            "resumed_bytes": checkpoint.resumed_bytes,
        }
        if checkpoint.kind == "file":
            return {
                **base,
                "bytes": checkpoint.payload_size,
                "sha256": checkpoint.payload_sha256,
            }
        return {
            **base,
            "archive_bytes": checkpoint.payload_size,
            "archive_sha256": checkpoint.payload_sha256,
            "entries": checkpoint.entries,
        }

    @staticmethod
    def _validate_checkpoint(
        checkpoint: SessionCopyCheckpoint,
        *,
        src: ControlSessionRecord,
        src_path: str,
        dst: ControlSessionRecord,
        dst_path: str,
        kind: CopyKind,
        overwrite: bool,
        chunk_size: int,
        owner_job_id: str | None,
    ) -> None:
        expected = {
            "source_session_id": str(src.session_id),
            "source_executor_id": str(src.executor_id),
            "destination_session_id": str(dst.session_id),
            "destination_executor_id": str(dst.executor_id),
            "source_path": src_path,
            "destination_path": dst_path,
            "overwrite": overwrite,
            "chunk_size": chunk_size,
            "owner_job_id": owner_job_id,
        }
        for field, value in expected.items():
            if getattr(checkpoint, field) != value:
                raise RuntimeError(
                    f"session-copy checkpoint {field} changed since export"
                )
        if kind != "auto" and checkpoint.kind != kind:
            raise RuntimeError(
                "session-copy checkpoint kind changed since export"
            )

    async def _copy_file(
        self,
        src: ControlSessionRecord,
        stat: TransferStatOutput,
        *,
        src_path: str,
        dst: ControlSessionRecord,
        dst_path: str,
        overwrite: bool,
        chunk_size: int,
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        if stat.size is None or stat.sha256 is None:
            raise RuntimeError("source file stat is missing size or sha256")
        copied = await self._stream_file(
            src,
            src_path,
            dst,
            dst_path,
            expected_bytes=stat.size,
            expected_sha256=stat.sha256,
            overwrite=overwrite,
            chunk_size=chunk_size,
            progress=progress,
        )
        return {
            "bytes": stat.size,
            "sha256": stat.sha256,
            "chunks": copied["chunks"],
            "chunk_size": chunk_size,
            "source_path": stat.path,
            "destination_path": copied["path"],
            "cleanup_errors": [],
        }

    async def _copy_dir(
        self,
        src: ControlSessionRecord,
        *,
        src_path: str,
        dst: ControlSessionRecord,
        dst_path: str,
        overwrite: bool,
        chunk_size: int,
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        await self._report(
            progress,
            phase="packing",
            bytes_transferred=0,
            total_bytes=0,
        )
        pack = TransferPackDirOutput.model_validate(
            await self._call(
                src,
                "transfer_pack_dir",
                {"path": src_path, "compression": "gz"},
            )
        )
        destination_temp: str | None = None
        cleanup_errors: list[str] = []
        try:
            allocated = TransferAllocTempPathOutput.model_validate(
                await self._call(
                    dst,
                    "transfer_alloc_temp_path",
                    {"suffix": ".tar.gz"},
                )
            )
            destination_temp = allocated.path
            copied = await self._stream_file(
                src,
                pack.archive_path,
                dst,
                destination_temp,
                expected_bytes=pack.bytes,
                expected_sha256=pack.sha256,
                overwrite=True,
                chunk_size=chunk_size,
                source_unbound_temp=True,
                destination_unbound_temp=True,
                progress=progress,
            )
            await self._report(
                progress,
                phase="unpacking",
                bytes_transferred=pack.bytes,
                total_bytes=pack.bytes,
            )
            unpack = TransferUnpackArchiveOutput.model_validate(
                await self._call(
                    dst,
                    "transfer_unpack_archive",
                    {
                        "archive_path": destination_temp,
                        "dst_path": dst_path,
                        "overwrite": overwrite,
                        "cleanup_archive": True,
                    },
                )
            )
            destination_temp = (
                None if unpack.archive_deleted else destination_temp
            )
            cleanup_errors.extend(unpack.cleanup_errors)
            return {
                "archive_bytes": pack.bytes,
                "archive_sha256": pack.sha256,
                "chunks": copied["chunks"],
                "chunk_size": chunk_size,
                "entries": unpack.entries,
                "source_path": pack.path,
                "destination_path": unpack.path,
                "cleanup_errors": cleanup_errors,
            }
        finally:
            try:
                await self._call(
                    src,
                    "transfer_delete_temp_path",
                    {"path": pack.archive_path},
                )
            except Exception as exc:
                cleanup_errors.append(f"source archive cleanup failed: {exc}")
            if destination_temp is not None:
                try:
                    await self._call(
                        dst,
                        "transfer_delete_temp_path",
                        {"path": destination_temp},
                    )
                except Exception as exc:
                    cleanup_errors.append(
                        f"destination archive cleanup failed: {exc}"
                    )

    async def _stream_file(
        self,
        src: ControlSessionRecord,
        src_path: str,
        dst: ControlSessionRecord,
        dst_path: str,
        *,
        expected_bytes: int,
        expected_sha256: str,
        overwrite: bool,
        chunk_size: int,
        source_unbound_temp: bool = False,
        destination_unbound_temp: bool = False,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        begin_args: dict[str, JsonValue] = {
            "path": dst_path,
            "overwrite": overwrite,
            "expected_bytes": expected_bytes,
        }
        if destination_unbound_temp:
            begin_args["_workgate_unbound_temp"] = True
        begin = TransferBeginWriteOutput.model_validate(
            await self._call(dst, "transfer_begin_write", begin_args)
        )
        transfer_id = begin.transfer_id
        offset = begin.offset
        chunks = 0
        await self._report(
            progress,
            phase="transferring",
            bytes_transferred=offset,
            total_bytes=expected_bytes,
            chunks=chunks,
            chunk_size=chunk_size,
        )
        try:
            while offset < expected_bytes:
                read_args: dict[str, JsonValue] = {
                    "path": src_path,
                    "offset": offset,
                    "chunk_size": chunk_size,
                }
                if source_unbound_temp:
                    read_args["_workgate_unbound_temp"] = True
                chunk = TransferReadChunkOutput.model_validate(
                    await self._call(src, "transfer_read_chunk", read_args)
                )
                if chunk.offset != offset or chunk.size != expected_bytes:
                    raise RuntimeError("source changed during session_copy")
                if chunk.bytes <= 0:
                    raise RuntimeError(
                        "source transfer made no forward progress"
                    )
                write_args: dict[str, JsonValue] = {
                    "path": dst_path,
                    "transfer_id": transfer_id,
                    "offset": offset,
                    "data_b64": chunk.data_b64,
                    "expected_sha256": chunk.sha256,
                }
                if destination_unbound_temp:
                    write_args["_workgate_unbound_temp"] = True
                await self._call(dst, "transfer_write_chunk", write_args)
                offset += chunk.bytes
                chunks += 1
                await self._report(
                    progress,
                    phase="transferring",
                    bytes_transferred=offset,
                    total_bytes=expected_bytes,
                    chunks=chunks,
                    chunk_size=chunk_size,
                )
            finish_args: dict[str, JsonValue] = {
                "path": dst_path,
                "transfer_id": transfer_id,
                "expected_bytes": expected_bytes,
                "expected_sha256": expected_sha256,
            }
            if destination_unbound_temp:
                finish_args["_workgate_unbound_temp"] = True
            finished = TransferFinishWriteOutput.model_validate(
                await self._call(dst, "transfer_finish_write", finish_args)
            )
            if not finished.completed or finished.bytes != expected_bytes:
                raise RuntimeError(
                    "destination transfer did not commit completely"
                )
            if finished.sha256 != expected_sha256:
                raise RuntimeError("destination transfer sha256 mismatch")
            return {"path": finished.path, "chunks": chunks}
        except BaseException:
            abort_args: dict[str, JsonValue] = {
                "path": dst_path,
                "transfer_id": transfer_id,
            }
            if destination_unbound_temp:
                abort_args["_workgate_unbound_temp"] = True
            with contextlib.suppress(Exception):
                await self._call(dst, "transfer_abort_write", abort_args)
            raise

    async def _call(
        self,
        record: ControlSessionRecord,
        op: str,
        args: dict[str, JsonValue],
    ) -> JsonValue:
        result = await self._transport.call(
            str(record.executor_id),
            op,
            args,
            session_id=str(record.session_id),
        )
        if result.ok:
            self._sessions.observe_session_activity(str(record.session_id))
            return result.result
        await self._sessions.reconcile_session_activity_after_error(record)
        assert result.error is not None
        raise RuntimeError(
            f"executor {op} failed: {result.error.code}: {result.error.message}"
        )

    @staticmethod
    async def _report(progress: ProgressCallback | None, **values: Any) -> None:
        if progress is not None:
            await progress(values)

    @staticmethod
    def _binding_snapshot(record: ControlSessionRecord) -> dict[str, str]:
        return {
            "executor_id": str(record.executor_id),
            "workdir": record.resolved_workdir_display
            or record.requested_workdir,
        }

    @classmethod
    def _validate_expected_binding(
        cls,
        record: ControlSessionRecord,
        expected: dict[str, str] | None,
        *,
        label: str,
    ) -> None:
        if expected is None:
            return
        current = cls._binding_snapshot(record)
        if current != expected:
            raise RuntimeError(
                f"{label} session binding changed since the managed copy was created"
            )

    @staticmethod
    def _resolve_kind(
        kind: CopyKind, stat: TransferStatOutput
    ) -> Literal["file", "dir"]:
        if kind == "auto":
            if stat.type not in {"file", "dir"}:
                raise ValueError(f"unsupported source path type: {stat.type}")
            return stat.type  # type: ignore[return-value]
        if stat.type != kind:
            raise ValueError(
                f"session_copy kind={kind!r} does not match source type {stat.type!r}"
            )
        return kind

    @staticmethod
    def _output(
        src: ControlSessionRecord,
        src_path: str,
        dst: ControlSessionRecord,
        dst_path: str,
        kind: Literal["file", "dir"],
        metrics: dict[str, Any],
    ) -> SessionCopyOutput:
        same_executor = src.executor_id == dst.executor_id
        return SessionCopyOutput(
            kind=kind,
            transport="same_executor" if same_executor else "control_payload",
            resumed_bytes=int(metrics.get("resumed_bytes", 0)),
            source=SessionCopyEndpoint(
                session_id=str(src.session_id),
                executor_id=str(src.executor_id),
                workdir=src.resolved_workdir_display or src.requested_workdir,
                path=src_path,
                resolved_path=metrics.get("source_path"),
            ),
            destination=SessionCopyEndpoint(
                session_id=str(dst.session_id),
                executor_id=str(dst.executor_id),
                workdir=dst.resolved_workdir_display or dst.requested_workdir,
                path=dst_path,
                resolved_path=metrics.get("destination_path"),
            ),
            relation=SessionCopyRelation(
                route="same_executor"
                if same_executor
                else "different_executors",
                same_session=src.session_id == dst.session_id,
                same_executor=same_executor,
            ),
            bytes=metrics.get("bytes"),
            sha256=metrics.get("sha256"),
            archive_bytes=metrics.get("archive_bytes"),
            archive_sha256=metrics.get("archive_sha256"),
            chunks=int(metrics.get("chunks", 0)),
            chunk_size=int(metrics["chunk_size"]),
            entries=metrics.get("entries"),
            cleanup_errors=list(metrics.get("cleanup_errors") or []),
        )
