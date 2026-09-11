"""Shared ASGI request-body limits for REST, MCP, OAuth, and remote routes."""

import contextlib
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..audit import audit


@dataclass(frozen=True)
class _BufferedRequest:
    """One bounded sequence of request messages ready for replay."""

    body: bytes
    """Complete accepted body bytes accumulated within the configured limit."""

    disconnected: bool
    """Whether the client disconnected before a complete request message."""

    bytes_received: int
    """Total body bytes across received HTTP request messages."""


@dataclass(frozen=True)
class _OversizedRequest:
    """Observed request size when the configured limit was crossed."""

    bytes_received: int
    """Exact cumulative body bytes received before rejection."""


class RequestBodyLimitMiddleware:
    """Reject HTTP bodies that exceed one configured byte budget."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max(0, int(max_bytes))

    @staticmethod
    def _declared_lengths(scope: Scope) -> tuple[int, ...]:
        values: list[int] = []
        for raw_name, raw_value in scope.get("headers", ()):  # type: ignore[misc]
            if raw_name.lower() != b"content-length":
                continue
            try:
                value = int(raw_value.decode("ascii").strip())
            except UnicodeDecodeError, ValueError:
                continue
            if value >= 0:
                values.append(value)
        return tuple(values)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        observed_bytes: int | None,
        declared_bytes: int | None,
    ) -> None:
        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "")
        with contextlib.suppress(Exception):
            audit(
                "http_request_rejected",
                reason="request_body_too_large",
                path=path,
                method=method,
                limit_bytes=self.max_bytes,
                observed_bytes=observed_bytes,
                declared_bytes=declared_bytes,
            )
        response = JSONResponse(
            {
                "error": "request_too_large",
                "message": (
                    "Request body exceeds the configured "
                    f"{self.max_bytes} byte limit"
                ),
                "limit_bytes": self.max_bytes,
            },
            status_code=413,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)

    async def _buffer_request(
        self,
        receive: Receive,
    ) -> _BufferedRequest | _OversizedRequest:
        body = bytearray()
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return _BufferedRequest(bytes(body), True, total)
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes | bytearray | memoryview):
                chunk = bytes(chunk)
            total += len(chunk)
            if total > self.max_bytes:
                return _OversizedRequest(total)
            body.extend(chunk)
            if not bool(message.get("more_body", False)):
                return _BufferedRequest(bytes(body), False, total)

    @staticmethod
    def _replay_receive(
        buffered: _BufferedRequest, receive: Receive
    ) -> Receive:
        body_replayed = False
        disconnect_replayed = False

        async def replay() -> Message:
            nonlocal body_replayed, disconnect_replayed
            if not body_replayed:
                body_replayed = True
                if buffered.disconnected and not buffered.body:
                    disconnect_replayed = True
                    return {"type": "http.disconnect"}
                return {
                    "type": "http.request",
                    "body": buffered.body,
                    "more_body": buffered.disconnected,
                }
            if buffered.disconnected and not disconnect_replayed:
                disconnect_replayed = True
                return {"type": "http.disconnect"}
            return await receive()

        return replay

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Buffer and replay one bounded HTTP body before downstream handling."""
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        declared_lengths = self._declared_lengths(scope)
        declared = max(declared_lengths, default=None)
        if declared is not None and declared > self.max_bytes:
            await self._reject(
                scope,
                receive,
                send,
                observed_bytes=None,
                declared_bytes=declared,
            )
            return

        buffered = await self._buffer_request(receive)
        if isinstance(buffered, _OversizedRequest):
            await self._reject(
                scope,
                receive,
                send,
                observed_bytes=buffered.bytes_received,
                declared_bytes=declared,
            )
            return
        await self.app(scope, self._replay_receive(buffered, receive), send)


def install_request_body_limit(app: Any, *, max_bytes: int) -> None:
    """Install the shared request-body middleware on a Starlette-compatible app."""
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=max_bytes)
