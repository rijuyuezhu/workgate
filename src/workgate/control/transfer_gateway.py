"""Private raw-byte transport for durable cross-executor session copy."""

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from ..protocol.executor import (
    EXECUTOR_TRANSFER_PREFIX,
    EXECUTOR_TRANSFER_TOKEN_HEADER,
)
from ..protocol.transfer import (
    DEFAULT_TRANSFER_CHUNK_BYTES,
    MAX_HTTP_TRANSFER_CHUNK_BYTES,
)
from .executor_transport import ExecutorTransport, ExecutorTransportError
from .payload_store import PayloadStore

_TRANSFER_CAPABILITY_TTL_S = 300.0
_HTTP_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class TransferCapabilityLease:
    """One transient transport credential delivered only to an executor command."""

    capability_id: str
    token: str
    path: str


@dataclass(frozen=True)
class _TransferCapability:
    capability_id: str
    token_sha256: bytes
    executor_id: str
    direction: Literal["upload", "download"]
    transfer_id: str
    expected_bytes: int
    expected_sha256: str
    offset: int
    expires_at: float
    staging_path: Path | None = None
    payload_id: str | None = None


@dataclass
class _ChunkUpload:
    """Short-lived, single-writer transfer state; the session-copy checkpoint owns durability."""

    offset: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    last_ack: tuple[int, int, str] | None = None
    complete: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ControlTransferGateway:
    """Own short-lived executor-bound capabilities for raw transfer bytes."""

    def __init__(
        self,
        transport: ExecutorTransport,
        payloads: PayloadStore,
        *,
        capability_ttl_s: float = _TRANSFER_CAPABILITY_TTL_S,
        clock=time.monotonic,
    ) -> None:
        self._transport = transport
        self._payloads = payloads
        self._capability_ttl_s = max(1.0, float(capability_ttl_s))
        self._clock = clock
        self._lock = threading.Lock()
        self._capabilities: dict[str, _TransferCapability] = {}
        self._chunk_uploads: dict[str, _ChunkUpload] = {}

    @staticmethod
    def _new_capability_id() -> str:
        return "xfer_" + secrets.token_urlsafe(16)

    @staticmethod
    def _new_token() -> str:
        return "wg_xfer_" + secrets.token_urlsafe(32)

    @staticmethod
    def _token_sha256(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def _issue(
        self,
        *,
        executor_id: str,
        direction: Literal["upload", "download"],
        transfer_id: str,
        expected_bytes: int,
        expected_sha256: str,
        offset: int,
        staging_path: Path | None = None,
        payload_id: str | None = None,
    ) -> TransferCapabilityLease:
        capability_id = self._new_capability_id()
        token = self._new_token()
        record = _TransferCapability(
            capability_id=capability_id,
            token_sha256=self._token_sha256(token),
            executor_id=str(executor_id),
            direction=direction,
            transfer_id=str(transfer_id),
            expected_bytes=int(expected_bytes),
            expected_sha256=str(expected_sha256),
            offset=int(offset),
            expires_at=self._clock() + self._capability_ttl_s,
            staging_path=staging_path,
            payload_id=payload_id,
        )
        with self._lock:
            self._capabilities[capability_id] = record
            if direction == "upload":
                self._chunk_uploads[capability_id] = _ChunkUpload()
        return TransferCapabilityLease(
            capability_id=capability_id,
            token=token,
            path=f"{EXECUTOR_TRANSFER_PREFIX}/{capability_id}",
        )

    def issue_upload(
        self,
        *,
        executor_id: str,
        transfer_id: str,
        staging_path: Path,
        expected_bytes: int,
        expected_sha256: str,
    ) -> TransferCapabilityLease:
        """Issue one single-claim upload capability."""
        return self._issue(
            executor_id=executor_id,
            direction="upload",
            transfer_id=transfer_id,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            offset=0,
            staging_path=staging_path,
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
    ) -> TransferCapabilityLease:
        """Issue one single-claim download capability."""
        if offset < 0 or offset > expected_bytes:
            raise ValueError("transfer download offset is invalid")
        return self._issue(
            executor_id=executor_id,
            direction="download",
            transfer_id=transfer_id,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            offset=offset,
            payload_id=payload_id,
        )

    def revoke(self, capability_id: str) -> None:
        """Drop one unclaimed process-local capability."""
        with self._lock:
            self._capabilities.pop(str(capability_id), None)
            self._chunk_uploads.pop(str(capability_id), None)

    def close(self) -> None:
        """Drop all process-local transport credentials."""
        with self._lock:
            self._capabilities.clear()
            self._chunk_uploads.clear()

    def _claim(
        self,
        *,
        capability_id: str,
        token: str,
        executor_id: str,
        direction: Literal["upload", "download"],
    ) -> _TransferCapability | None:
        now = self._clock()
        with self._lock:
            record = self._capabilities.get(capability_id)
            if record is None:
                return None
            if record.expires_at <= now:
                self._capabilities.pop(capability_id, None)
                self._chunk_uploads.pop(capability_id, None)
                return None
            if (
                record.executor_id != executor_id
                or record.direction != direction
                or not hmac.compare_digest(
                    record.token_sha256, self._token_sha256(token)
                )
            ):
                return None
            self._capabilities.pop(capability_id, None)
            self._chunk_uploads.pop(capability_id, None)
            return record

    @staticmethod
    def _bearer(request: Request) -> str:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return ""
        return value

    def _authenticated_executor(self, request: Request) -> str | None:
        try:
            return self._transport.authenticate_live_bearer(
                self._bearer(request)
            )
        except ExecutorTransportError:
            return None

    @staticmethod
    def _missing() -> JSONResponse:
        return JSONResponse(
            {"error": "transfer_capability_not_found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    def _chunk_claim(
        self, capability_id: str, token: str, executor_id: str
    ) -> tuple[_TransferCapability, _ChunkUpload] | None:
        with self._lock:
            record = self._capabilities.get(capability_id)
            if record is None:
                return None
            if record.expires_at <= self._clock():
                self._capabilities.pop(capability_id, None)
                self._chunk_uploads.pop(capability_id, None)
                return None
            if (
                record.direction != "upload"
                or record.executor_id != executor_id
                or not hmac.compare_digest(
                    record.token_sha256, self._token_sha256(token)
                )
            ):
                return None
            progress = self._chunk_uploads.get(capability_id)
            return (record, progress) if progress is not None else None

    @staticmethod
    def _chunk_ack(
        record: _TransferCapability, progress: _ChunkUpload
    ) -> JSONResponse:
        return JSONResponse(
            {
                "offset": progress.offset,
                "bytes": progress.offset,
                "completed": progress.complete,
                "sha256": record.expected_sha256 if progress.complete else "",
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _upload_chunk(
        self,
        request: Request,
        record: _TransferCapability,
        progress: _ChunkUpload,
    ) -> Response:
        """Accept bounded, resumable HTTP ranges under one Executor-bound lease."""
        staging = record.staging_path
        if staging is None:
            return self._missing()
        raw_range = request.headers.get("content-range", "")
        match = _HTTP_CONTENT_RANGE_RE.fullmatch(raw_range)
        if match is None:
            return JSONResponse(
                {"error": "transfer_range_invalid"}, status_code=409
            )
        start, last, total = (int(value) for value in match.groups())
        size = last - start + 1
        if total != record.expected_bytes or size <= 0 or last >= total:
            return JSONResponse(
                {"error": "transfer_range_invalid"}, status_code=409
            )
        if size > MAX_HTTP_TRANSFER_CHUNK_BYTES:
            return JSONResponse(
                {"error": "transfer_chunk_too_large"}, status_code=413
            )
        chunk_sha = request.headers.get("x-chunk-sha256", "").lower()
        if _SHA256_RE.fullmatch(chunk_sha) is None:
            return JSONResponse(
                {"error": "transfer_chunk_digest_invalid"}, status_code=409
            )
        declared = request.headers.get("content-length")
        if declared is not None and declared != str(size):
            return JSONResponse(
                {"error": "transfer_size_mismatch"}, status_code=409
            )

        async with progress.lock:
            # Recheck replay bytes without reapplying the already-committed range.
            if progress.last_ack == (start, last + 1, chunk_sha):
                replay_digest = hashlib.sha256()
                replay_bytes = 0
                async for chunk in request.stream():
                    replay_bytes += len(chunk)
                    if replay_bytes > size:
                        break
                    replay_digest.update(chunk)
                if (
                    replay_bytes != size
                    or replay_digest.hexdigest() != chunk_sha
                ):
                    return JSONResponse(
                        {"error": "transfer_chunk_integrity_mismatch"},
                        status_code=409,
                    )
                return self._chunk_ack(record, progress)
            if progress.complete or start != progress.offset:
                return JSONResponse(
                    {
                        "error": "transfer_offset_mismatch",
                        "offset": progress.offset,
                    },
                    status_code=409,
                )
            next_digest = progress.digest.copy()
            chunk_digest = hashlib.sha256()
            received = 0
            try:
                if progress.offset == 0 and not staging.exists():
                    handle = self._payloads.open_private_staging(
                        staging, namespace="transfer"
                    )
                else:
                    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(staging, flags)
                    handle = os.fdopen(descriptor, "wb")
                with handle:
                    stage_stat = os.fstat(handle.fileno())
                    if (
                        not stat.S_ISREG(stage_stat.st_mode)
                        or stage_stat.st_size != progress.offset
                    ):
                        raise ValueError(
                            "transfer staging changed between chunks"
                        )
                    handle.seek(progress.offset)
                    committed = False
                    try:
                        async for chunk in request.stream():
                            if not chunk:
                                continue
                            received += len(chunk)
                            if received > size:
                                break
                            chunk_digest.update(chunk)
                            next_digest.update(chunk)
                            await asyncio.to_thread(handle.write, chunk)
                        if (
                            received != size
                            or chunk_digest.hexdigest() != chunk_sha
                        ):
                            return JSONResponse(
                                {"error": "transfer_chunk_integrity_mismatch"},
                                status_code=409,
                            )
                        await asyncio.to_thread(handle.flush)
                        await asyncio.to_thread(os.fsync, handle.fileno())
                        committed = True
                    finally:
                        # A failed/interrupted request never commits a partial chunk.
                        if not committed:
                            handle.truncate(progress.offset)
            except OSError, ValueError:
                # A compromised or removed staging file cannot be resumed.
                self.revoke(record.capability_id)
                staging.unlink(missing_ok=True)
                return JSONResponse(
                    {"error": "transfer_staging_unavailable"}, status_code=409
                )

            if (
                last + 1 == record.expected_bytes
                and next_digest.hexdigest() != record.expected_sha256
            ):
                self.revoke(record.capability_id)
                staging.unlink(missing_ok=True)
                return JSONResponse(
                    {"error": "transfer_integrity_mismatch"}, status_code=409
                )
            progress.offset = last + 1
            progress.digest = next_digest
            progress.last_ack = (start, last + 1, chunk_sha)
            progress.complete = progress.offset == record.expected_bytes
            with self._lock:
                # Never resurrect a capability revoked by its parent copy job.
                current = self._capabilities.get(record.capability_id)
                if current is not None:
                    self._capabilities[record.capability_id] = replace(
                        current,
                        expires_at=self._clock() + self._capability_ttl_s,
                    )
            return self._chunk_ack(record, progress)

    async def _upload(
        self, request: Request, record: _TransferCapability
    ) -> Response:
        staging = record.staging_path
        if staging is None:
            return self._missing()
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) != record.expected_bytes:
                    return JSONResponse(
                        {"error": "transfer_size_mismatch"},
                        status_code=409,
                        headers={"Cache-Control": "no-store"},
                    )
            except ValueError:
                return JSONResponse(
                    {"error": "transfer_size_mismatch"},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )

        digest = hashlib.sha256()
        received = 0
        oversized = False
        try:
            with self._payloads.open_private_staging(
                staging, namespace="transfer"
            ) as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > record.expected_bytes:
                        oversized = True
                        break
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
                if not oversized:
                    await asyncio.to_thread(handle.flush)
                    await asyncio.to_thread(os.fsync, handle.fileno())
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

        if oversized:
            staging.unlink(missing_ok=True)
            return JSONResponse(
                {"error": "transfer_size_mismatch"},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        if (
            received != record.expected_bytes
            or digest.hexdigest() != record.expected_sha256
        ):
            staging.unlink(missing_ok=True)
            return JSONResponse(
                {"error": "transfer_integrity_mismatch"},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"bytes": received, "sha256": record.expected_sha256},
            headers={"Cache-Control": "no-store"},
        )

    async def _download(self, record: _TransferCapability) -> Response:
        payload_id = record.payload_id
        if payload_id is None:
            return self._missing()
        try:
            handle, _path = await asyncio.to_thread(
                self._payloads.open_payload,
                payload_id,
                namespace="transfer",
                size=record.expected_bytes,
                sha256=record.expected_sha256,
            )
            handle.seek(record.offset)
        except OSError, ValueError:
            return JSONResponse(
                {"error": "transfer_payload_unavailable"},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

        async def stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await asyncio.to_thread(
                        handle.read, DEFAULT_TRANSFER_CHUNK_BYTES
                    )
                    if not chunk:
                        break
                    yield chunk
            finally:
                handle.close()

        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "Content-Length": str(record.expected_bytes - record.offset),
                "X-Workgate-Transfer-Offset": str(record.offset),
            },
        )

    async def endpoint(self, request: Request) -> Response:
        """Serve one single-claim authenticated raw transfer."""
        executor_id = self._authenticated_executor(request)
        if executor_id is None:
            return self._missing()
        capability_id = str(request.path_params.get("capability_id") or "")
        token = request.headers.get(EXECUTOR_TRANSFER_TOKEN_HEADER, "")
        direction: Literal["upload", "download"] = (
            "upload" if request.method.upper() == "PUT" else "download"
        )
        if (
            direction == "upload"
            and request.headers.get("content-range") is not None
        ):
            claimed = self._chunk_claim(capability_id, token, executor_id)
            if claimed is None:
                return self._missing()
            return await self._upload_chunk(request, *claimed)
        record = self._claim(
            capability_id=capability_id,
            token=token,
            executor_id=executor_id,
            direction=direction,
        )
        if record is None:
            return self._missing()
        if direction == "upload":
            return await self._upload(request, record)
        return await self._download(record)

    def routes(self) -> list[BaseRoute]:
        """Return private executor routes for both control HTTP hosts."""
        return [
            Route(
                f"{EXECUTOR_TRANSFER_PREFIX}/{{capability_id}}",
                self.endpoint,
                methods=["GET", "PUT"],
            )
        ]
