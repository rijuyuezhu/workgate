"""Capacity limits for stateful Streamable HTTP MCP sessions."""

import asyncio
from collections.abc import MutableMapping
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ...audit import audit


class McpSessionLimitMiddleware:
    """Reject new stateful MCP sessions once the configured capacity is full."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        session_manager: Any,
        max_sessions: int,
        mcp_path: str = "/mcp",
    ) -> None:
        self.app = app
        self.session_manager = session_manager
        self.max_sessions = max(1, int(max_sessions))
        self.mcp_path = mcp_path
        self._creation_lock = asyncio.Lock()
        self._pending_creations = 0
        self._validate_manager_compatibility()

    def _validate_manager_compatibility(self) -> None:
        """Fail loudly when the installed MCP SDK changes required internals."""
        if bool(getattr(self.session_manager, "stateless", False)):
            return
        instances = getattr(self.session_manager, "_server_instances", None)
        if not isinstance(instances, MutableMapping):
            raise RuntimeError(
                "Installed MCP SDK is incompatible with stateful session limits: "
                "session manager has no mutable _server_instances mapping"
            )
        owners = getattr(self.session_manager, "_session_owners", None)
        if owners is not None and not isinstance(owners, MutableMapping):
            raise RuntimeError(
                "Installed MCP SDK is incompatible with stateful session limits: "
                "_session_owners is not a mutable mapping"
            )

    def _is_new_session(self, scope: Scope) -> bool:
        """Return whether this request can create one stateful MCP session."""
        if (
            scope.get("type") != "http"
            or scope.get("path") != self.mcp_path
            or str(scope.get("method") or "").upper() != "POST"
        ):
            return False
        header_names = {
            bytes(name).lower()
            for name, _value in scope.get("headers", ())  # type: ignore[misc]
        }
        return b"mcp-session-id" not in header_names

    def _active_session_count(self) -> int:
        """Count live SDK sessions and discard terminated bookkeeping entries."""
        instances = self.session_manager._server_instances
        owners = getattr(self.session_manager, "_session_owners", None)
        pruned = 0
        for session_id, transport in list(instances.items()):
            if not bool(getattr(transport, "is_terminated", False)):
                continue
            instances.pop(session_id, None)
            if isinstance(owners, MutableMapping):
                owners.pop(session_id, None)
            pruned += 1
        if pruned:
            audit(
                "mcp_session_bookkeeping_pruned",
                pruned_sessions=pruned,
                active_sessions=len(instances),
            )
        return len(instances)

    async def _release_reservation(self) -> None:
        """Release one pending creation reservation exactly once."""
        async with self._creation_lock:
            self._pending_creations = max(0, self._pending_creations - 1)

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Reserve capacity without serializing the complete initialize request."""
        if bool(
            getattr(self.session_manager, "stateless", False)
        ) or not self._is_new_session(scope):
            await self.app(scope, receive, send)
            return

        async with self._creation_lock:
            active_sessions = self._active_session_count()
            occupied = active_sessions + self._pending_creations
            if occupied >= self.max_sessions:
                audit(
                    "mcp_session_rejected",
                    reason="session_limit",
                    active_sessions=active_sessions,
                    pending_creations=self._pending_creations,
                    max_sessions=self.max_sessions,
                )
                response = JSONResponse(
                    {
                        "ok": False,
                        "error": "mcp_session_limit",
                        "message": (
                            f"MCP session limit reached: {self.max_sessions}"
                        ),
                        "limit": self.max_sessions,
                    },
                    status_code=429,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
            self._pending_creations += 1

        reservation_active = True
        reservation_release_task: asyncio.Task[None] | None = None

        async def release_active_reservation() -> None:
            """Finish one release before propagating caller cancellation."""
            nonlocal reservation_active, reservation_release_task
            if not reservation_active:
                return
            if reservation_release_task is None:
                reservation_release_task = asyncio.create_task(
                    self._release_reservation()
                )
            cancellation: asyncio.CancelledError | None = None
            while not reservation_release_task.done():
                try:
                    await asyncio.shield(reservation_release_task)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
            reservation_release_task.result()
            reservation_active = False
            if cancellation is not None:
                raise cancellation

        async def send_with_release(message: Message) -> None:
            if (
                message.get("type") == "http.response.start"
                and reservation_active
            ):
                headers = message.get("headers", ())
                if any(
                    bytes(name).lower() == b"mcp-session-id"
                    for name, _ in headers
                ):
                    await release_active_reservation()
                    audit(
                        "mcp_session_created",
                        active_sessions=self._active_session_count(),
                        max_sessions=self.max_sessions,
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_with_release)
        finally:
            if reservation_active:
                await release_active_reservation()
