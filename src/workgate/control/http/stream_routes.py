"""Public WebSocket rendezvous routes for ephemeral terminal streams."""

from __future__ import annotations

import re

from starlette.routing import BaseRoute, WebSocketRoute
from starlette.websockets import WebSocket

from ...protocol.terminal import (
    BROWSER_TERMINAL_STREAM_ROUTE,
    EXECUTOR_TERMINAL_STREAM_ROUTE,
    TERMINAL_BROWSER_SUBPROTOCOL,
    TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX,
)
from ..executor_transport import ExecutorTransport, ExecutorTransportError
from ..streams import ControlStreamHub

_STREAM_ID_PATTERN = re.compile(r"^stream_[A-Za-z0-9_-]{20,128}$")


def _stream_id(websocket: WebSocket) -> str:
    value = str(websocket.path_params.get("stream_id") or "")
    if not _STREAM_ID_PATTERN.fullmatch(value):
        return ""
    return value


def _bearer(websocket: WebSocket) -> str:
    header = websocket.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return ""
    return value


def _browser_protocols(websocket: WebSocket) -> tuple[str | None, str]:
    offered = tuple(
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(
            ","
        )
        if value.strip()
    )
    subprotocol = (
        TERMINAL_BROWSER_SUBPROTOCOL
        if TERMINAL_BROWSER_SUBPROTOCOL in offered
        else None
    )
    token = next(
        (
            value.removeprefix(TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX)
            for value in offered
            if value.startswith(TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX)
        ),
        "",
    )
    return subprotocol, token


def terminal_stream_routes(
    transport: ExecutorTransport,
    hub: ControlStreamHub,
) -> list[BaseRoute]:
    """Return public executor/browser WebSocket endpoints for one StreamHub."""

    async def executor_stream(websocket: WebSocket) -> None:
        stream_id = _stream_id(websocket)
        if not stream_id:
            await websocket.close(code=4404, reason="Terminal stream not found")
            return
        try:
            executor_id = transport.authenticate_live_bearer(_bearer(websocket))
        except ExecutorTransportError as exc:
            await websocket.close(code=4403, reason=exc.error.message[:120])
            return
        expected = hub.expected_executor(stream_id)
        if expected is None:
            await websocket.close(code=4404, reason="Terminal stream not found")
            return
        if expected != executor_id:
            await websocket.close(
                code=4403,
                reason="Terminal stream belongs to another executor",
            )
            return
        await websocket.accept()
        if not await hub.claim_executor(stream_id, executor_id, websocket):
            await websocket.close(
                code=4409, reason="Terminal stream unavailable"
            )
            return
        try:
            await websocket.send_json(
                {"type": "stream-accepted", "stream_id": stream_id}
            )
        except Exception:
            await hub.cancel(stream_id)
            return
        if not await hub.activate_executor(stream_id):
            await hub.cancel(stream_id)
            return
        await hub.wait_closed(stream_id)

    async def browser_stream(websocket: WebSocket) -> None:
        stream_id = _stream_id(websocket)
        subprotocol, token = _browser_protocols(websocket)
        if not stream_id or not token:
            await websocket.close(
                code=4401, reason="Terminal attach token required"
            )
            return
        if subprotocol != TERMINAL_BROWSER_SUBPROTOCOL:
            await websocket.close(
                code=4400,
                reason="Terminal stream subprotocol is required",
            )
            return
        await websocket.accept(subprotocol=subprotocol)
        if not await hub.claim_browser(stream_id, token, websocket):
            await websocket.close(
                code=4401,
                reason="Terminal attach token is invalid, expired, or already used",
            )
            return
        await hub.wait_closed(stream_id)

    return [
        WebSocketRoute(EXECUTOR_TERMINAL_STREAM_ROUTE, executor_stream),
        WebSocketRoute(BROWSER_TERMINAL_STREAM_ROUTE, browser_stream),
    ]
