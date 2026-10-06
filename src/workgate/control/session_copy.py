"""Control-orchestrated copy between two existing execution sessions."""

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
    TransferCopyFileOutput,
    TransferFinishWriteOutput,
    TransferPackDirOutput,
    TransferStatOutput,
    TransferUnpackArchiveOutput,
)
from .executor_transport import ExecutorTransport
from .object_store_transfer import S3ObjectTransferService
from .payload_store import PayloadStore
from .session_copy_store import (
    SessionCopyCheckpoint,
    SessionCopyCheckpointStore,
)
from .sessions import ControlSessionCoordinator
from .state import ControlSessionRecord
from .transfer_gateway import ControlTransferGateway

CopyKind = Literal["auto", "file", "dir"]
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
SESSION_COPY_MANAGED_KIND = "session-copy"
_ABANDONMENT_RPC_TIMEOUT_S = 30.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PreparedCrossExecutorSource:
    kind: Literal["file", "dir"]
    transfer_path: str
    source_resolved_path: str
    payload_size: int
    payload_sha256: str
    unbound_temp: bool
    cleanup_path: str | None = None


class _ObjectStoreRouteUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ControlSessionCopyService:
    """Copy data without creating, migrating, or rebinding either session."""

    def __init__(
        self,
        sessions: ControlSessionCoordinator,
        transport: ExecutorTransport,
        state_store: StateStore,
        data_dir: Path,
        *,
        transfer_gateway: ControlTransferGateway,
        object_store: S3ObjectTransferService,
        max_transfer_payload_bytes: int,
        max_transfer_payload_store_bytes: int,
    ) -> None:
        self._sessions = sessions
        self._transport = transport
        self._payloads = PayloadStore(data_dir)
        self._transfer_gateway = transfer_gateway
        self._object_store = object_store
        self._max_transfer_payload_bytes = int(max_transfer_payload_bytes)
        self._max_transfer_payload_store_bytes = int(
            max_transfer_payload_store_bytes
        )
        self._checkpoints = SessionCopyCheckpointStore(
            state_store, self._payloads
        )
        self._abandonment_tasks: dict[str, asyncio.Task[None]] = {}
        self._abandonment_rerun: set[str] = set()
        self._closed = False

    async def aclose(self) -> None:
        """Cancel process-local abandonment reconciliation tasks."""
        self._closed = True
        tasks = tuple(self._abandonment_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._abandonment_tasks.clear()
        self._abandonment_rerun.clear()

    async def reconcile_object_store_orphans(self) -> None:
        """Retry cleanup for temporary object-store keys left by prior attempts."""
        errors = await self._object_store.reconcile_orphans()
        for error in errors:
            logger.warning("%s", error)

    def schedule_reconcile_abandonments(self, *, executor_id: str) -> None:
        """Run post-hello cleanup without blocking the hello response/poll startup."""
        if self._closed:
            return
        existing = self._abandonment_tasks.get(executor_id)
        if existing is not None and not existing.done():
            self._abandonment_rerun.add(executor_id)
            return
        task = asyncio.create_task(
            self._run_scheduled_abandonments(executor_id),
            name=f"workgate-session-copy-abandon-{executor_id}",
        )
        self._abandonment_tasks[executor_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._abandonment_tasks.get(executor_id) is not completed:
                return
            self._abandonment_tasks.pop(executor_id, None)
            rerun = executor_id in self._abandonment_rerun
            self._abandonment_rerun.discard(executor_id)
            if rerun and not self._closed:
                self.schedule_reconcile_abandonments(executor_id=executor_id)

        task.add_done_callback(discard)

    async def _run_scheduled_abandonments(self, executor_id: str) -> None:
        await asyncio.sleep(0)
        with contextlib.suppress(Exception):
            await self.reconcile_abandonments(executor_id=executor_id)

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
        await self.reconcile_object_store_orphans()
        await self.reconcile_abandonments()
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
                    (
                        resolved_kind,
                        metrics,
                        selected_route,
                    ) = await self._copy_cross_executor(
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
                        transport=selected_route,
                    )
                finally:
                    if owner_job_id is None:
                        with contextlib.suppress(Exception):
                            abandoned = self._checkpoints.prepare_abandonment(
                                active_transfer_id
                            )
                            if abandoned is not None and (
                                abandoned.last_known_step == "exported"
                                or abandoned.receipts_released
                                or await self._sessions.session_availability(
                                    str(dst.session_id)
                                )
                                == "available"
                            ):
                                await self._reconcile_abandonment(abandoned)
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
                transport="same_executor",
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
        """Start one durable control-managed copy under execution-session admission."""
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
        """Return this runtime's execution-session-aware managed-copy handler."""
        return SESSION_COPY_MANAGED_KIND, self._run_managed_job

    async def reconcile_abandonments(
        self, *, executor_id: str | None = None
    ) -> None:
        """Finish feature-local cleanup before dropping transfer authority."""
        checkpoints = self._checkpoints.prepare_abandonments(
            executor_id=executor_id
        )
        for checkpoint in checkpoints:
            with contextlib.suppress(Exception):
                await self._reconcile_abandonment(checkpoint)

    async def _reconcile_abandonment(
        self, checkpoint: SessionCopyCheckpoint
    ) -> None:
        if (
            checkpoint.last_known_step == "exported"
            or checkpoint.receipts_released
        ):
            self._checkpoints.remove(checkpoint.transfer_id)
            return
        if checkpoint.import_path is None:
            raise RuntimeError(
                "abandoning import checkpoint is missing import path"
            )
        abandon_args: dict[str, JsonValue] = {
            "transfer_id": checkpoint.transfer_id,
            "kind": checkpoint.kind,
            "import_path": checkpoint.import_path,
        }
        result = await self._transport.call(
            str(checkpoint.destination_executor_id),
            "transfer_abandon_import",
            abandon_args,
            timeout_s=_ABANDONMENT_RPC_TIMEOUT_S,
        )
        if not result.ok:
            assert result.error is not None
            raise RuntimeError(
                "executor transfer_abandon_import failed: "
                f"{result.error.code}: {result.error.message}"
            )
        if (
            not isinstance(result.result, dict)
            or result.result.get("safe_to_forget") is not True
        ):
            raise RuntimeError(
                "executor transfer_abandon_import did not confirm safe cleanup"
            )
        self._checkpoints.remove(checkpoint.transfer_id)

    def retry_require_available(
        self, job_id: str, session_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Return executor availability required by one managed-copy retry."""
        checkpoint = self._checkpoints.load_for_owner_job(job_id)
        if checkpoint is None:
            return session_ids
        if checkpoint.abandoning:
            raise RuntimeError("session-copy checkpoint is being abandoned")
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

    async def _prepare_cross_executor_source(
        self,
        src: ControlSessionRecord,
        *,
        src_path: str,
        kind: CopyKind,
        progress: ProgressCallback | None,
    ) -> _PreparedCrossExecutorSource:
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
            if stat.size is None or stat.sha256 is None:
                raise RuntimeError("source file stat is missing size or sha256")
            return _PreparedCrossExecutorSource(
                kind="file",
                transfer_path=src_path,
                source_resolved_path=stat.path,
                payload_size=stat.size,
                payload_sha256=stat.sha256,
                unbound_temp=False,
            )

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
        return _PreparedCrossExecutorSource(
            kind="dir",
            transfer_path=pack.archive_path,
            source_resolved_path=pack.path,
            payload_size=pack.bytes,
            payload_sha256=pack.sha256,
            unbound_temp=True,
            cleanup_path=pack.archive_path,
        )

    async def _cleanup_prepared_cross_executor_source(
        self,
        src: ControlSessionRecord,
        prepared: _PreparedCrossExecutorSource,
        cleanup_errors: list[str],
    ) -> None:
        if prepared.cleanup_path is None:
            return
        try:
            await self._call(
                src,
                "transfer_delete_temp_path",
                {"path": prepared.cleanup_path},
            )
        except Exception as exc:
            cleanup_errors.append(
                f"source archive cleanup failed: {type(exc).__name__}"
            )

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
    ) -> tuple[
        Literal["file", "dir"],
        dict[str, Any],
        Literal["object_store", "control_relay"],
    ]:
        if checkpoint is not None:
            (
                resolved_kind,
                metrics,
            ) = await self._copy_cross_executor_via_control(
                src,
                src_path=src_path,
                dst=dst,
                dst_path=dst_path,
                overwrite=overwrite,
                chunk_size=chunk_size,
                progress=progress,
                transfer_id=transfer_id,
                owner_job_id=owner_job_id,
                checkpoint=checkpoint,
                prepared=None,
                cleanup_errors=[],
            )
            return resolved_kind, metrics, "control_relay"

        prepared = await self._prepare_cross_executor_source(
            src,
            src_path=src_path,
            kind=kind,
            progress=progress,
        )
        cleanup_errors: list[str] = []
        fallbacks: list[str] = []
        selected_kind: Literal["file", "dir"]
        selected_metrics: dict[str, Any]
        selected_route: Literal["object_store", "control_relay"]
        try:
            if self._object_store.enabled:
                try:
                    selected_metrics = (
                        await self._copy_cross_executor_via_object_store(
                            src,
                            prepared=prepared,
                            dst=dst,
                            dst_path=dst_path,
                            overwrite=overwrite,
                            chunk_size=chunk_size,
                            progress=progress,
                            transfer_id=transfer_id,
                            cleanup_errors=cleanup_errors,
                        )
                    )
                except _ObjectStoreRouteUnavailable as exc:
                    fallbacks.append(f"object_store:{exc.reason}")
                else:
                    selected_kind = prepared.kind
                    selected_route = "object_store"
                    selected_metrics["cleanup_errors"] = cleanup_errors
                    selected_metrics["fallbacks"] = fallbacks
                    return selected_kind, selected_metrics, selected_route

            (
                selected_kind,
                selected_metrics,
            ) = await self._copy_cross_executor_via_control(
                src,
                src_path=src_path,
                dst=dst,
                dst_path=dst_path,
                overwrite=overwrite,
                chunk_size=chunk_size,
                progress=progress,
                transfer_id=transfer_id,
                owner_job_id=owner_job_id,
                checkpoint=None,
                prepared=prepared,
                cleanup_errors=cleanup_errors,
            )
            selected_route = "control_relay"
            selected_metrics["cleanup_errors"] = cleanup_errors
            selected_metrics["fallbacks"] = fallbacks
            return selected_kind, selected_metrics, selected_route
        finally:
            await self._cleanup_prepared_cross_executor_source(
                src, prepared, cleanup_errors
            )
            current = self._checkpoints.load(transfer_id)
            if current is not None and cleanup_errors != current.cleanup_errors:
                with contextlib.suppress(Exception):
                    self._checkpoints.update(
                        transfer_id,
                        cleanup_errors=cleanup_errors,
                    )

    async def _call_object_store_route(
        self,
        record: ControlSessionRecord,
        op: str,
        args: dict[str, JsonValue],
        *,
        stage: str,
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
        if result.error.code == "transfer_route_unavailable":
            raise _ObjectStoreRouteUnavailable(stage)
        raise RuntimeError(f"executor {op} failed: {result.error.code}")

    async def _abandon_object_store_import(
        self,
        dst: ControlSessionRecord,
        *,
        import_path: str,
        transfer_id: str,
        kind: Literal["file", "dir"],
    ) -> None:
        result = await self._call(
            dst,
            "transfer_abandon_import",
            {
                "transfer_id": transfer_id,
                "kind": kind,
                "import_path": import_path,
            },
        )
        if (
            not isinstance(result, dict)
            or result.get("safe_to_forget") is not True
        ):
            raise RuntimeError(
                "executor transfer_abandon_import did not confirm safe cleanup"
            )

    async def _copy_cross_executor_via_object_store(
        self,
        src: ControlSessionRecord,
        *,
        prepared: _PreparedCrossExecutorSource,
        dst: ControlSessionRecord,
        dst_path: str,
        overwrite: bool,
        chunk_size: int,
        progress: ProgressCallback | None,
        transfer_id: str,
        cleanup_errors: list[str],
    ) -> dict[str, Any]:
        try:
            attempt = self._object_store.begin_attempt(transfer_id)
        except Exception as exc:
            raise _ObjectStoreRouteUnavailable("setup") from exc

        cleanup_error: str | None = None
        import_path: str | None = None
        destination_cleanup_needed = False
        try:
            try:
                put_url = self._object_store.presign_put(attempt)
            except Exception as exc:
                raise _ObjectStoreRouteUnavailable("presign_put") from exc

            await self._report(
                progress,
                phase="transferring",
                bytes_transferred=0,
                total_bytes=prepared.payload_size,
                chunks=0,
                chunk_size=chunk_size,
            )
            uploaded = await self._call_object_store_route(
                src,
                "transfer.url_upload",
                {
                    "transfer_id": transfer_id,
                    "path": prepared.transfer_path,
                    "expected_bytes": prepared.payload_size,
                    "expected_sha256": prepared.payload_sha256,
                    "chunk_size": chunk_size,
                    "url": put_url,
                    "_workgate_unbound_temp": prepared.unbound_temp,
                },
                stage="upload",
            )
            uploaded_bytes = (
                uploaded.get("bytes") if isinstance(uploaded, dict) else None
            )
            uploaded_sha256 = (
                uploaded.get("sha256") if isinstance(uploaded, dict) else None
            )
            if (
                not isinstance(uploaded_bytes, int)
                or isinstance(uploaded_bytes, bool)
                or uploaded_bytes != prepared.payload_size
                or not isinstance(uploaded_sha256, str)
                or uploaded_sha256 != prepared.payload_sha256
            ):
                raise RuntimeError(
                    "executor object-store upload acknowledgement is invalid"
                )

            try:
                get_url = self._object_store.presign_get(attempt)
            except Exception as exc:
                raise _ObjectStoreRouteUnavailable("presign_get") from exc

            unbound_temp = prepared.kind == "dir"
            if unbound_temp:
                allocated = TransferAllocTempPathOutput.model_validate(
                    await self._call(
                        dst,
                        "transfer_alloc_temp_path",
                        {"suffix": ".tar.gz"},
                    )
                )
                import_path = allocated.path
            else:
                import_path = dst_path

            begin_args: dict[str, JsonValue] = {
                "path": import_path,
                "overwrite": True if unbound_temp else overwrite,
                "expected_bytes": prepared.payload_size,
                "transfer_id": transfer_id,
            }
            if unbound_temp:
                begin_args["_workgate_unbound_temp"] = True

            begin = TransferBeginWriteOutput.model_validate(
                await self._call(dst, "transfer_begin_write", begin_args)
            )
            if begin.offset < 0 or begin.offset > prepared.payload_size:
                raise RuntimeError(
                    "destination transfer resume offset is invalid"
                )
            resumed_bytes = begin.offset
            destination_cleanup_needed = (
                prepared.kind == "dir" or not begin.completed
            )

            if not begin.completed:
                download_error: _ObjectStoreRouteUnavailable | None = None
                for attempt_index in range(2):
                    try:
                        downloaded = await self._call_object_store_route(
                            dst,
                            "transfer.url_download",
                            {
                                "transfer_id": begin.transfer_id,
                                "path": import_path,
                                "expected_bytes": prepared.payload_size,
                                "chunk_size": chunk_size,
                                "offset": begin.offset,
                                "url": get_url,
                                "_workgate_unbound_temp": unbound_temp,
                            },
                            stage="download",
                        )
                    except _ObjectStoreRouteUnavailable as exc:
                        download_error = exc
                        if attempt_index:
                            break
                        begin = TransferBeginWriteOutput.model_validate(
                            await self._call(
                                dst, "transfer_begin_write", begin_args
                            )
                        )
                        resumed_bytes = max(resumed_bytes, begin.offset)
                        if (
                            begin.completed
                            or begin.offset == prepared.payload_size
                        ):
                            download_error = None
                            break
                        try:
                            get_url = self._object_store.presign_get(attempt)
                        except Exception:
                            download_error = _ObjectStoreRouteUnavailable(
                                "presign_get"
                            )
                            break
                        continue

                    downloaded_offset = (
                        downloaded.get("offset")
                        if isinstance(downloaded, dict)
                        else None
                    )
                    if (
                        not isinstance(downloaded_offset, int)
                        or isinstance(downloaded_offset, bool)
                        or downloaded_offset != prepared.payload_size
                    ):
                        raise RuntimeError(
                            "executor object-store download "
                            "acknowledgement is invalid"
                        )
                    download_error = None
                    break

                if download_error is not None:
                    raise download_error

            if begin.completed:
                if (
                    begin.offset != prepared.payload_size
                    or begin.sha256 != prepared.payload_sha256
                ):
                    raise RuntimeError(
                        "destination transfer receipt integrity mismatch"
                    )
                destination_path = begin.path
            else:
                finish_args: dict[str, JsonValue] = {
                    "path": import_path,
                    "transfer_id": begin.transfer_id,
                    "expected_bytes": prepared.payload_size,
                    "expected_sha256": prepared.payload_sha256,
                }
                if unbound_temp:
                    finish_args["_workgate_unbound_temp"] = True
                try:
                    finished = TransferFinishWriteOutput.model_validate(
                        await self._call(
                            dst, "transfer_finish_write", finish_args
                        )
                    )
                except Exception as original:
                    try:
                        recovered = TransferBeginWriteOutput.model_validate(
                            await self._call(
                                dst, "transfer_begin_write", begin_args
                            )
                        )
                    except Exception:
                        raise original from None
                    if (
                        not recovered.completed
                        or recovered.offset != prepared.payload_size
                        or recovered.sha256 != prepared.payload_sha256
                    ):
                        raise original from None
                    destination_path = recovered.path
                else:
                    if (
                        not finished.completed
                        or finished.bytes != prepared.payload_size
                        or finished.sha256 != prepared.payload_sha256
                    ):
                        raise RuntimeError(
                            "destination transfer did not commit completely"
                        )
                    destination_path = finished.path
                destination_cleanup_needed = prepared.kind == "dir"
            if begin.completed:
                destination_cleanup_needed = prepared.kind == "dir"

            chunks = (
                0
                if prepared.payload_size == 0
                else (prepared.payload_size + chunk_size - 1) // chunk_size
            )
            await self._report(
                progress,
                phase="transferring",
                bytes_transferred=prepared.payload_size,
                total_bytes=prepared.payload_size,
                chunks=chunks,
                chunk_size=chunk_size,
                resumed_bytes=resumed_bytes,
            )

            entries: int | None = None
            if prepared.kind == "dir":
                await self._report(
                    progress,
                    phase="unpacking",
                    bytes_transferred=prepared.payload_size,
                    total_bytes=prepared.payload_size,
                    chunks=chunks,
                    chunk_size=chunk_size,
                    resumed_bytes=resumed_bytes,
                )
                unpack_args: dict[str, JsonValue] = {
                    "archive_path": import_path,
                    "dst_path": dst_path,
                    "overwrite": overwrite,
                    "cleanup_archive": True,
                    "transfer_id": transfer_id,
                    "expected_archive_bytes": prepared.payload_size,
                    "expected_archive_sha256": prepared.payload_sha256,
                }
                try:
                    unpack_raw = await self._call(
                        dst, "transfer_unpack_archive", unpack_args
                    )
                except Exception:
                    unpack_raw = await self._call(
                        dst, "transfer_unpack_archive", unpack_args
                    )
                unpack = TransferUnpackArchiveOutput.model_validate(unpack_raw)
                cleanup_errors.extend(unpack.cleanup_errors)
                destination_path = unpack.path
                entries = unpack.entries
                destination_cleanup_needed = False

            try:
                await self._call(
                    dst,
                    "transfer_release_receipts",
                    {"transfer_id": transfer_id},
                )
            except Exception as exc:
                cleanup_errors.append(
                    "destination transfer receipt cleanup failed: "
                    f"{type(exc).__name__}"
                )

            base = {
                "chunks": chunks,
                "chunk_size": chunk_size,
                "source_path": prepared.source_resolved_path,
                "destination_path": destination_path,
                "cleanup_errors": cleanup_errors,
                "resumed_bytes": resumed_bytes,
            }
            if prepared.kind == "file":
                return {
                    **base,
                    "bytes": prepared.payload_size,
                    "sha256": prepared.payload_sha256,
                }
            return {
                **base,
                "archive_bytes": prepared.payload_size,
                "archive_sha256": prepared.payload_sha256,
                "entries": entries,
            }
        except _ObjectStoreRouteUnavailable:
            if destination_cleanup_needed and import_path is not None:
                await self._abandon_object_store_import(
                    dst,
                    import_path=import_path,
                    transfer_id=transfer_id,
                    kind=prepared.kind,
                )
            raise
        except Exception:
            if destination_cleanup_needed and import_path is not None:
                with contextlib.suppress(Exception):
                    await self._abandon_object_store_import(
                        dst,
                        import_path=import_path,
                        transfer_id=transfer_id,
                        kind=prepared.kind,
                    )
            raise
        finally:
            cleanup_error = await self._object_store.finish_attempt(attempt)
            if cleanup_error is not None:
                message = f"object-store cleanup failed ({cleanup_error})"
                cleanup_errors.append(message)
                logger.warning("%s", message)

    async def _copy_cross_executor_via_control(
        self,
        src: ControlSessionRecord,
        *,
        src_path: str,
        dst: ControlSessionRecord,
        dst_path: str,
        overwrite: bool,
        chunk_size: int,
        progress: ProgressCallback | None,
        transfer_id: str,
        owner_job_id: str | None,
        checkpoint: SessionCopyCheckpoint | None,
        prepared: _PreparedCrossExecutorSource | None,
        cleanup_errors: list[str],
    ) -> tuple[Literal["file", "dir"], dict[str, Any]]:
        """Run the durable control-retained fallback route."""
        current = checkpoint
        if current is None:
            if prepared is None:
                raise RuntimeError(
                    "control-relay export requires a prepared source"
                )
            current = await self._export_payload(
                src,
                export_path=prepared.transfer_path,
                source_resolved_path=prepared.source_resolved_path,
                payload_size=prepared.payload_size,
                payload_sha256=prepared.payload_sha256,
                source_unbound_temp=prepared.unbound_temp,
                src_path=src_path,
                dst=dst,
                dst_path=dst_path,
                kind=prepared.kind,
                overwrite=overwrite,
                chunk_size=chunk_size,
                transfer_id=transfer_id,
                owner_job_id=owner_job_id,
                progress=progress,
                cleanup_errors=cleanup_errors,
            )
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
        self._checkpoints.reserve_export(
            transfer_id=transfer_id,
            owner_job_id=owner_job_id,
            payload_size=payload_size,
            max_payload_bytes=self._max_transfer_payload_bytes,
            max_store_bytes=self._max_transfer_payload_store_bytes,
        )
        staging = self._payloads.new_staging_path("transfer")
        lease = self._transfer_gateway.issue_upload(
            executor_id=str(src.executor_id),
            transfer_id=transfer_id,
            staging_path=staging,
            expected_bytes=payload_size,
            expected_sha256=payload_sha256,
        )
        try:
            await self._report(
                progress,
                phase="exporting",
                bytes_transferred=0,
                total_bytes=payload_size,
                chunk_size=chunk_size,
            )
            uploaded = await self._call(
                src,
                "transfer.http_upload",
                {
                    "transfer_id": transfer_id,
                    "path": export_path,
                    "expected_bytes": payload_size,
                    "expected_sha256": payload_sha256,
                    "chunk_size": chunk_size,
                    "capability_path": lease.path,
                    "capability_token": lease.token,
                    "_workgate_unbound_temp": source_unbound_temp,
                },
            )
            uploaded_bytes = (
                uploaded.get("bytes") if isinstance(uploaded, dict) else None
            )
            uploaded_sha256 = (
                uploaded.get("sha256") if isinstance(uploaded, dict) else None
            )
            if (
                not isinstance(uploaded_bytes, int)
                or isinstance(uploaded_bytes, bool)
                or uploaded_bytes != payload_size
                or not isinstance(uploaded_sha256, str)
                or uploaded_sha256 != payload_sha256
            ):
                raise RuntimeError(
                    "executor raw transfer upload acknowledgement is invalid"
                )
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
            self._transfer_gateway.revoke(lease.capability_id)
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
        else:
            checkpoint = self._checkpoints.update(
                checkpoint.transfer_id,
                receipts_released=True,
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
        begin_args: dict[str, JsonValue] = {
            "path": import_path,
            "overwrite": True if unbound_temp else checkpoint.overwrite,
            "expected_bytes": checkpoint.payload_size,
            "transfer_id": checkpoint.import_resource_id
            or checkpoint.transfer_id,
        }
        if unbound_temp:
            begin_args["_workgate_unbound_temp"] = True
        begin = TransferBeginWriteOutput.model_validate(
            await self._call(dst, "transfer_begin_write", begin_args)
        )
        if begin.offset < 0 or begin.offset > checkpoint.payload_size:
            raise RuntimeError("destination transfer resume offset is invalid")
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

        await self._report(
            progress,
            phase="importing",
            bytes_transferred=begin.offset,
            total_bytes=checkpoint.payload_size,
            chunk_size=checkpoint.chunk_size,
            resumed_bytes=resumed_bytes,
        )
        lease = self._transfer_gateway.issue_download(
            executor_id=str(dst.executor_id),
            transfer_id=checkpoint.transfer_id,
            payload_id=str(checkpoint.payload_id),
            expected_bytes=checkpoint.payload_size,
            expected_sha256=checkpoint.payload_sha256,
            offset=begin.offset,
        )
        try:
            downloaded = await self._call(
                dst,
                "transfer.http_download",
                {
                    "transfer_id": begin.transfer_id,
                    "path": import_path,
                    "expected_bytes": checkpoint.payload_size,
                    "chunk_size": checkpoint.chunk_size,
                    "offset": begin.offset,
                    "capability_path": lease.path,
                    "capability_token": lease.token,
                    "_workgate_unbound_temp": unbound_temp,
                },
            )
        finally:
            self._transfer_gateway.revoke(lease.capability_id)
        downloaded_offset = (
            downloaded.get("offset") if isinstance(downloaded, dict) else None
        )
        if (
            not isinstance(downloaded_offset, int)
            or isinstance(downloaded_offset, bool)
            or downloaded_offset != checkpoint.payload_size
        ):
            raise RuntimeError(
                "executor raw transfer download acknowledgement is invalid"
            )
        await self._report(
            progress,
            phase="importing",
            bytes_transferred=checkpoint.payload_size,
            total_bytes=checkpoint.payload_size,
            chunk_size=checkpoint.chunk_size,
            resumed_bytes=resumed_bytes,
        )

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
        if checkpoint.abandoning:
            raise RuntimeError("session-copy checkpoint is being abandoned")
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
        src_binding = self._binding_snapshot(src)
        dst_binding = self._binding_snapshot(dst)
        await self._report(
            progress,
            phase="transferring",
            bytes_transferred=0,
            total_bytes=stat.size,
            chunks=0,
            chunk_size=chunk_size,
        )
        try:
            copied = TransferCopyFileOutput.model_validate(
                await self._call(
                    src,
                    "transfer_copy_file",
                    {
                        "source_path": src_path,
                        "destination_path": dst_path,
                        "overwrite": overwrite,
                        "chunk_size": chunk_size,
                        "expected_bytes": stat.size,
                        "expected_sha256": stat.sha256,
                        "source_session_id": str(src.session_id),
                        "destination_session_id": str(dst.session_id),
                        "source_workdir": src_binding["workdir"],
                        "destination_workdir": dst_binding["workdir"],
                    },
                )
            )
        except BaseException:
            await self._sessions.reconcile_session_activity_after_error(dst)
            raise
        self._sessions.observe_session_activity(str(dst.session_id))
        if not copied.completed:
            raise RuntimeError("executor-local file copy did not complete")
        if copied.bytes != stat.size or copied.sha256 != stat.sha256:
            raise RuntimeError("executor-local file copy integrity mismatch")
        await self._report(
            progress,
            phase="transferring",
            bytes_transferred=copied.bytes,
            total_bytes=copied.bytes,
            chunks=copied.chunks,
            chunk_size=copied.chunk_size,
        )
        return {
            "bytes": copied.bytes,
            "sha256": copied.sha256,
            "chunks": copied.chunks,
            "chunk_size": copied.chunk_size,
            "source_path": copied.source_path,
            "destination_path": copied.path,
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
        cleanup_errors: list[str] = []
        chunks = (
            0
            if pack.bytes == 0
            else (pack.bytes + chunk_size - 1) // chunk_size
        )
        try:
            await self._report(
                progress,
                phase="transferring",
                bytes_transferred=0,
                total_bytes=pack.bytes,
                chunks=0,
                chunk_size=chunk_size,
            )
            await self._report(
                progress,
                phase="transferring",
                bytes_transferred=pack.bytes,
                total_bytes=pack.bytes,
                chunks=chunks,
                chunk_size=chunk_size,
            )
            await self._report(
                progress,
                phase="unpacking",
                bytes_transferred=pack.bytes,
                total_bytes=pack.bytes,
                chunks=chunks,
                chunk_size=chunk_size,
            )
            unpack = TransferUnpackArchiveOutput.model_validate(
                await self._call(
                    dst,
                    "transfer_unpack_archive",
                    {
                        "archive_path": pack.archive_path,
                        "dst_path": dst_path,
                        "overwrite": overwrite,
                        "cleanup_archive": True,
                        "expected_archive_bytes": pack.bytes,
                        "expected_archive_sha256": pack.sha256,
                    },
                )
            )
            cleanup_errors.extend(unpack.cleanup_errors)
            return {
                "archive_bytes": pack.bytes,
                "archive_sha256": pack.sha256,
                "chunks": chunks,
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
        if record.workdir is None:
            raise RuntimeError(
                f"session {record.session_id} has no confirmed workdir"
            )
        return {
            "executor_id": str(record.executor_id),
            "workdir": record.workdir,
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
        *,
        transport: Literal["same_executor", "object_store", "control_relay"],
    ) -> SessionCopyOutput:
        same_executor = src.executor_id == dst.executor_id
        source_binding = ControlSessionCopyService._binding_snapshot(src)
        destination_binding = ControlSessionCopyService._binding_snapshot(dst)
        return SessionCopyOutput(
            kind=kind,
            transport=transport,
            resumed_bytes=int(metrics.get("resumed_bytes", 0)),
            source=SessionCopyEndpoint(
                session_id=str(src.session_id),
                executor_id=source_binding["executor_id"],
                workdir=source_binding["workdir"],
                path=src_path,
            ),
            destination=SessionCopyEndpoint(
                session_id=str(dst.session_id),
                executor_id=destination_binding["executor_id"],
                workdir=destination_binding["workdir"],
                path=dst_path,
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
            fallbacks=list(metrics.get("fallbacks") or []),
        )
