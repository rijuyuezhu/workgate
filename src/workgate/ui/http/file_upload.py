"""Authenticated bounded browser uploads using existing Executor transfer writes."""

import asyncio
import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import HTTPException
from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import Response

from ...control.ui_executor import call_ui_executor
from ...oauth.core.context import current_oauth_claims
from ...oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from .common import json_error, json_ok, require_scopes
from .file_download import _MAX_FILE_BYTES
from .files import _executor_id_arg, _path_arg

_UPLOAD_CHUNK_BYTES = 256 * 1024
_UPLOAD_IDLE_TTL_S = 15 * 60
_MAX_ACTIVE_UPLOADS = 16


@dataclass
class _Upload:
    executor_id: str
    path: str
    transfer_id: str
    size: int
    expires_at: float
    owner: tuple[str, str, str]
    offset: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserUploadTransfers:
    """Per-Control browser transfer authority, not a second file staging system."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._uploads: dict[str, _Upload] = {}

    def available(self) -> bool:
        now = time.monotonic()
        with self._guard:
            self._uploads = {
                token: record
                for token, record in self._uploads.items()
                if record.expires_at > now
            }
            return len(self._uploads) < _MAX_ACTIVE_UPLOADS

    def issue(
        self,
        *,
        executor_id: str,
        path: str,
        transfer_id: str,
        size: int,
        owner: tuple[str, str, str],
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._guard:
            if len(self._uploads) >= _MAX_ACTIVE_UPLOADS:
                raise ValueError("Too many active browser uploads")
            self._uploads[token] = _Upload(
                executor_id=executor_id,
                path=path,
                transfer_id=transfer_id,
                size=size,
                expires_at=now + _UPLOAD_IDLE_TTL_S,
                owner=owner,
            )
        return token

    def get(self, token: str) -> _Upload | None:
        with self._guard:
            record = self._uploads.get(token)
            if record is None:
                return None
            if record.expires_at <= time.monotonic():
                self._uploads.pop(token, None)
                return None
            return record

    def touch(self, record: _Upload) -> None:
        record.expires_at = time.monotonic() + _UPLOAD_IDLE_TTL_S

    def discard(self, token: str) -> None:
        with self._guard:
            self._uploads.pop(token, None)


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("WebUI Files requires Control runtime")
    return runtime


def _store(request: Request) -> BrowserUploadTransfers:
    return cast(BrowserUploadTransfers, request.app.state.browser_uploads)


def _upload_owner(request: Request) -> tuple[str, str, str]:
    claims = current_oauth_claims()
    if claims is None:
        return ("local", "", "")
    identity = str(claims.get("jti") or "")
    if not identity:
        identity = hashlib.sha256(
            request.headers.get("authorization", "").encode("utf-8")
        ).hexdigest()
    return (
        str(claims.get("sub") or ""),
        str(claims.get("client_id") or ""),
        identity,
    )


def _upload(request: Request) -> tuple[str, _Upload]:
    require_scopes(SCOPE_SHELL_READ, SCOPE_SHELL_WRITE)
    token = str(request.path_params.get("token") or "")
    record = _store(request).get(token)
    if record is None or record.owner != _upload_owner(request):
        raise HTTPException(
            status_code=404, detail="Upload no longer available"
        )
    return token, record


def _success(data: dict[str, Any]) -> Response:
    response = json_ok(data)
    response.headers["Cache-Control"] = "no-store"
    return response


async def api_file_upload_start(request: Request) -> Response:
    """Prepare an Executor-owned transactional write, forbidding overwrite."""
    try:
        require_scopes(SCOPE_SHELL_READ, SCOPE_SHELL_WRITE)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Upload request must be an object")
        executor_id = _executor_id_arg(payload.get("executor_id"))
        path = _path_arg(payload.get("path"))
        size = payload.get("size")
        if type(size) is not int or not 0 <= size <= _MAX_FILE_BYTES:
            raise ValueError("Upload size is invalid or exceeds the limit")
        registry = _store(request)
        if not registry.available():
            raise ValueError("Too many active browser uploads")
        runtime = _runtime(request)
        resolved, reply = await call_ui_executor(
            runtime,
            executor_id,
            "transfer_begin_write",
            {"path": path, "overwrite": False, "expected_bytes": size},
        )
        if not isinstance(reply, dict) or not isinstance(
            reply.get("transfer_id"), str
        ):
            raise RuntimeError("Executor returned invalid upload transaction")
        transfer_id = str(reply["transfer_id"])
        destination = str(reply["path"])
        try:
            token = registry.issue(
                executor_id=resolved,
                path=destination,
                transfer_id=transfer_id,
                size=size,
                owner=_upload_owner(request),
            )
        except Exception:
            await call_ui_executor(
                runtime,
                resolved,
                "transfer_abort_write",
                {"path": destination, "transfer_id": transfer_id},
            )
            raise
        return _success({"token": token, "chunk_bytes": _UPLOAD_CHUNK_BYTES})
    except HTTPException:
        raise
    except Exception as exc:
        return json_error(exc)


async def api_file_upload_chunk(request: Request) -> Response:
    """Forward at most 256KiB raw HTTP body to one existing staged write."""
    try:
        token, record = _upload(request)
        raw_offset = request.headers.get("x-workgate-upload-offset", "")
        if not raw_offset.isascii() or not raw_offset.isdecimal():
            raise ValueError("Upload offset is invalid")
        offset = int(raw_offset)
        size_header = request.headers.get("content-length")
        if size_header is not None and int(size_header) > _UPLOAD_CHUNK_BYTES:
            raise HTTPException(
                status_code=413, detail="Upload chunk is too large"
            )
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _UPLOAD_CHUNK_BYTES:
                raise HTTPException(
                    status_code=413, detail="Upload chunk is too large"
                )
            body.extend(chunk)
        if not body:
            raise ValueError("Upload chunk is empty")
        async with record.lock:
            if _store(request).get(token) is not record:
                raise HTTPException(
                    status_code=404, detail="Upload no longer available"
                )
            if offset != record.offset or offset + len(body) > record.size:
                raise ValueError("Upload offset or length mismatch")
            digest = hashlib.sha256(body).hexdigest()
            args: dict[str, JsonValue] = {
                "path": record.path,
                "transfer_id": record.transfer_id,
                "offset": offset,
                "data_b64": base64.b64encode(body).decode("ascii"),
                "expected_sha256": digest,
            }
            executor_id, result = await call_ui_executor(
                _runtime(request),
                record.executor_id,
                "transfer_write_chunk",
                args,
            )
            if (
                executor_id != record.executor_id
                or not isinstance(result, dict)
                or result.get("offset") != offset
                or result.get("bytes") != len(body)
                or result.get("sha256") != digest
            ):
                raise RuntimeError("Executor upload acknowledgement mismatch")
            record.offset += len(body)
            record.digest.update(body)
            _store(request).touch(record)
            return _success({"offset": record.offset, "size": record.size})
    except HTTPException:
        raise
    except Exception as exc:
        return json_error(exc)


async def api_file_upload_finish(request: Request) -> Response:
    """Validate end-to-end digest and publish the staged destination atomically."""
    try:
        token, record = _upload(request)
        async with record.lock:
            if _store(request).get(token) is not record:
                raise HTTPException(
                    status_code=404, detail="Upload no longer available"
                )
            if record.offset != record.size:
                raise ValueError("Upload is incomplete")
            expected = record.digest.hexdigest()
            executor_id, result = await call_ui_executor(
                _runtime(request),
                record.executor_id,
                "transfer_finish_write",
                {
                    "path": record.path,
                    "transfer_id": record.transfer_id,
                    "expected_bytes": record.size,
                    "expected_sha256": expected,
                },
            )
            if (
                executor_id != record.executor_id
                or not isinstance(result, dict)
                or result.get("completed") is not True
                or result.get("bytes") != record.size
                or result.get("sha256") != expected
            ):
                raise RuntimeError("Executor upload completion mismatch")
            _store(request).discard(token)
            return _success({"path": record.path, "bytes": record.size})
    except HTTPException:
        raise
    except Exception as exc:
        return json_error(exc)


async def api_file_upload_abort(request: Request) -> Response:
    """Abort the Executor staging transaction after cancellation or failure."""
    try:
        token, record = _upload(request)
        async with record.lock:
            await call_ui_executor(
                _runtime(request),
                record.executor_id,
                "transfer_abort_write",
                {"path": record.path, "transfer_id": record.transfer_id},
            )
            _store(request).discard(token)
            return _success({"aborted": True})
    except HTTPException:
        raise
    except Exception as exc:
        return json_error(exc)
