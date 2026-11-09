"""One-use browser downloads over bounded Executor file RPCs."""

import asyncio
import base64
import binascii
import hashlib
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from ...control.ui_executor import call_ui_executor
from ...oauth.core.scopes import SCOPE_SHELL_READ
from ..security import UI_API_PREFIX
from .common import json_error, json_ok, require_scopes
from .files import _executor_id_arg, _path_arg

_DOWNLOAD_CHUNK_BYTES = 256 * 1024
_DOWNLOAD_TICKET_TTL_S = 60
_MAX_OUTSTANDING_TICKETS = 32
_MAX_FILE_BYTES = 8 * 1024**3
_SINGLE_RANGE = re.compile(r"bytes=(\d*)-(\d*)\Z")


@dataclass(frozen=True)
class DownloadTicket:
    executor_id: str
    path: str
    name: str
    size: int
    identity: list[int]
    expires_at: float


class BrowserDownloadTickets:
    """Process-local, bounded, single-use file capabilities; never Executor credentials."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, DownloadTicket] = {}
        self._slots = asyncio.Semaphore(4)

    def issue(self, *, executor_id: str, info: dict[str, Any]) -> str:
        size = int(info["size"])
        identity = info["identity"]
        if not 0 <= size <= _MAX_FILE_BYTES:
            raise ValueError("File exceeds the browser download limit")
        if (
            not isinstance(identity, list)
            or len(identity) != 5
            or any(type(part) is not int for part in identity)
        ):
            raise ValueError("Executor returned an invalid file identity")
        name = str(info["name"])
        if (
            not name
            or len(name.encode("utf-8")) > 255
            or any(c in name for c in "/\\\r\n\x00")
        ):
            raise ValueError("Executor returned an invalid download filename")
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._tickets = {
                key: value
                for key, value in self._tickets.items()
                if value.expires_at > now
            }
            if len(self._tickets) >= _MAX_OUTSTANDING_TICKETS:
                raise ValueError("Too many pending browser downloads")
            self._tickets[token] = DownloadTicket(
                executor_id=executor_id,
                path=str(info["path"]),
                name=name,
                size=size,
                identity=identity,
                expires_at=now + _DOWNLOAD_TICKET_TTL_S,
            )
        return token

    def consume(self, token: str) -> DownloadTicket | None:
        with self._lock:
            ticket = self._tickets.pop(token, None)
        if ticket is None or ticket.expires_at <= time.monotonic():
            return None
        return ticket

    @property
    def stream_slots(self) -> asyncio.Semaphore:
        return self._slots


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("WebUI Files requires Control runtime")
    return runtime


async def api_file_download_ticket(request: Request) -> Response:
    """Exchange an authorized file selection for a short one-use browser URL."""
    try:
        require_scopes(SCOPE_SHELL_READ)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        executor_id = _executor_id_arg(body.get("executor_id"))
        path = _path_arg(body.get("path"))
        runtime = _runtime(request)
        resolved, result = await call_ui_executor(
            runtime, executor_id, "ui.files.download.info", {"path": path}
        )
        if not isinstance(result, dict):
            raise ValueError("Executor returned invalid file metadata")
        token = request.app.state.browser_download_tickets.issue(
            executor_id=resolved, info=result
        )
        response = json_ok({"url": f"{UI_API_PREFIX}/files/download/{token}"})
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    except HTTPException:
        raise
    except Exception as exc:
        return json_error(exc)


def _range_bounds(header: str | None, size: int) -> tuple[int, int, bool]:
    if header is None:
        return 0, size, False
    match = _SINGLE_RANGE.fullmatch(header)
    if match is None or not (match.group(1) or match.group(2)):
        raise ValueError("Invalid single byte range")
    start, stop = match.groups()
    if start:
        first = int(start)
        last = size - 1 if not stop else min(int(stop), size - 1)
        if first > last or first >= size:
            raise ValueError("Range is unsatisfiable")
        return first, last + 1, True
    suffix = int(stop)
    if suffix <= 0 or size <= 0:
        raise ValueError("Range is unsatisfiable")
    return max(0, size - suffix), size, True


async def api_file_download(request: Request) -> Response:
    """Stream an attachment through one consumed, limited browser capability."""
    store: BrowserDownloadTickets = request.app.state.browser_download_tickets
    ticket = store.consume(str(request.path_params.get("ticket") or ""))
    if ticket is None:
        return Response(
            "Not found", status_code=404, headers={"Cache-Control": "no-store"}
        )
    try:
        start, end, partial = _range_bounds(
            request.headers.get("range"), ticket.size
        )
    except ValueError:
        return Response(
            "Range not satisfiable",
            status_code=416,
            headers={
                "Content-Range": f"bytes */{ticket.size}",
                "Cache-Control": "no-store",
            },
        )

    runtime = _runtime(request)
    try:
        actual_executor, metadata = await call_ui_executor(
            runtime,
            ticket.executor_id,
            "ui.files.download.info",
            {"path": ticket.path},
        )
        if (
            actual_executor != ticket.executor_id
            or not isinstance(metadata, dict)
            or metadata.get("identity") != ticket.identity
            or metadata.get("size") != ticket.size
        ):
            raise ValueError("File changed since download was requested")
    except Exception:
        return Response(
            "Download no longer available",
            status_code=409,
            headers={"Cache-Control": "no-store"},
        )

    async def chunks() -> AsyncIterator[bytes]:
        async with store.stream_slots:
            offset = start
            while offset < end:
                length = min(_DOWNLOAD_CHUNK_BYTES, end - offset)
                args: dict[str, JsonValue] = {
                    "path": ticket.path,
                    "identity": cast(JsonValue, ticket.identity),
                    "offset": offset,
                    "length": length,
                }
                async with asyncio.timeout(30):
                    resolved, result = await call_ui_executor(
                        runtime,
                        ticket.executor_id,
                        "ui.files.download.chunk",
                        args,
                    )
                if resolved != ticket.executor_id or not isinstance(
                    result, dict
                ):
                    raise RuntimeError("Download target changed")
                payload = result.get("data_base64")
                if not isinstance(payload, str) or len(payload) > 4 * (
                    (length + 2) // 3
                ):
                    raise RuntimeError("Invalid Executor file chunk")
                try:
                    data = base64.b64decode(payload, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise RuntimeError("Invalid Executor file chunk") from exc
                if (
                    len(data) != length
                    or result.get("offset") != offset
                    or result.get("bytes") != length
                    or hashlib.sha256(data).hexdigest() != result.get("sha256")
                ):
                    raise RuntimeError(
                        "Executor file chunk integrity check failed"
                    )
                yield data
                offset += length

    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"attachment; filename=\"download\"; filename*=UTF-8''{quote(ticket.name, safe='')}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start),
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{ticket.size}"
    return StreamingResponse(
        chunks(),
        status_code=206 if partial else 200,
        media_type="application/octet-stream",
        headers=headers,
    )
