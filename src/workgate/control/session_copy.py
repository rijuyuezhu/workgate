"""Control-orchestrated copy between two existing final shared sessions."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import PurePath
from typing import Any, Literal, cast

from pydantic import JsonValue

from ..jobs.managed import (
    ManagedJobContext,
    ManagedJobHandler,
    start_managed_job_without_session_admission,
)
from ..ops.transfer import normalize_chunk_size
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
    ) -> None:
        self._sessions = sessions
        self._transport = transport

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
    ) -> SessionCopyOutput:
        chunk_bytes = normalize_chunk_size(chunk_size)
        async with self._sessions.session_admission(
            (src_session_id, dst_session_id)
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
            transport="same_worker" if same_executor else "worker_rpc",
            resumed_bytes=0,
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
