"""Authenticated ephemeral terminal and Desktop WebSocket rendezvous routes."""

import re

from starlette.routing import BaseRoute, WebSocketRoute
from starlette.websockets import WebSocket

from ...protocol.desktop import (
    BROWSER_DESKTOP_STREAM_ROUTE,
    DESKTOP_BROWSER_SUBPROTOCOL,
    DESKTOP_BROWSER_TOKEN_PREFIX,
    EXECUTOR_DESKTOP_STREAM_ROUTE,
)
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


def _browser_protocols(
    websocket: WebSocket,
    *,
    subprotocol: str = TERMINAL_BROWSER_SUBPROTOCOL,
    token_prefix: str = TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX,
) -> tuple[str | None, str]:
    offered = tuple(
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(
            ","
        )
        if value.strip()
    )
    selected_protocol = subprotocol if subprotocol in offered else None
    token = next(
        (
            value.removeprefix(token_prefix)
            for value in offered
            if value.startswith(token_prefix)
        ),
        "",
    )
    return selected_protocol, token


def _paired_stream_routes(
    transport: ExecutorTransport,
    hub: ControlStreamHub,
    *,
    executor_route: str,
    browser_route: str,
    browser_protocol: str,
    token_prefix: str,
    label: str,
) -> list[BaseRoute]:
    """Connect one distinct authenticated stream hub to its public WS paths."""

    async def executor_stream(websocket: WebSocket) -> None:
        stream_id = _stream_id(websocket)
        if not stream_id:
            await websocket.close(code=4404, reason=f"{label} stream not found")
            return
        try:
            executor_id = transport.authenticate_live_bearer(_bearer(websocket))
        except ExecutorTransportError as exc:
            await websocket.close(code=4403, reason=exc.error.message[:120])
            return
        expected = hub.expected_executor(stream_id)
        if expected is None:
            await websocket.close(code=4404, reason=f"{label} stream not found")
            return
        if expected != executor_id:
            await websocket.close(
                code=4403,
                reason=f"{label} stream belongs to another executor",
            )
            return
        await websocket.accept()
        if not await hub.claim_executor(stream_id, executor_id, websocket):
            await websocket.close(
                code=4409, reason=f"{label} stream unavailable"
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
        subprotocol, token = _browser_protocols(
            websocket, subprotocol=browser_protocol, token_prefix=token_prefix
        )
        if not stream_id or not token:
            await websocket.close(
                code=4401, reason=f"{label} attach token required"
            )
            return
        if subprotocol != browser_protocol:
            await websocket.close(
                code=4400,
                reason=f"{label} stream subprotocol is required",
            )
            return
        await websocket.accept(subprotocol=subprotocol)
        if not await hub.claim_browser(stream_id, token, websocket):
            await websocket.close(
                code=4401,
                reason=f"{label} attach token is invalid, expired, or already used",
            )
            return
        await hub.wait_closed(stream_id)

    return [
        WebSocketRoute(executor_route, executor_stream),
        WebSocketRoute(browser_route, browser_stream),
    ]


def terminal_stream_routes(
    transport: ExecutorTransport, hub: ControlStreamHub
) -> list[BaseRoute]:
    return _paired_stream_routes(
        transport,
        hub,
        executor_route=EXECUTOR_TERMINAL_STREAM_ROUTE,
        browser_route=BROWSER_TERMINAL_STREAM_ROUTE,
        browser_protocol=TERMINAL_BROWSER_SUBPROTOCOL,
        token_prefix=TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX,
        label="Terminal",
    )


def desktop_stream_routes(
    transport: ExecutorTransport, hub: ControlStreamHub
) -> list[BaseRoute]:
    return _paired_stream_routes(
        transport,
        hub,
        executor_route=EXECUTOR_DESKTOP_STREAM_ROUTE,
        browser_route=BROWSER_DESKTOP_STREAM_ROUTE,
        browser_protocol=DESKTOP_BROWSER_SUBPROTOCOL,
        token_prefix=DESKTOP_BROWSER_TOKEN_PREFIX,
        label="Desktop",
    )
