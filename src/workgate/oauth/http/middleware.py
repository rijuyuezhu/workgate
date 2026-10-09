"""ASGI middleware for OAuth-protected HTTP and MCP requests."""

from collections.abc import Iterable

import jwt
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Receive, Scope, Send

from ...ui.http.session import has_valid_ui_csrf, ui_session_claims
from ...ui.security import is_ui_api_path
from ..core.context import bind_oauth_claims, reset_oauth_claims
from .auth import request_authentication_is_bypassed, verify_request


def _public_route_matches(route: BaseRoute, scope: Scope) -> bool:
    """Return whether a configured public route fully matches the request scope."""
    match, _ = route.matches(scope)
    return match is Match.FULL


class AuthMiddleware:
    """ASGI middleware for OAuth bearer verification."""

    def __init__(
        self, app: ASGIApp, *, public_routes: Iterable[BaseRoute] = ()
    ) -> None:
        self.app = app
        self._public_routes = tuple(public_routes)

    def _is_public_scope(self, scope: Scope) -> bool:
        """Return whether a request scope is served by a configured public route."""
        return any(
            _public_route_matches(route, scope) for route in self._public_routes
        )

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Apply public-route bypasses and bearer verification."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._is_public_scope(scope):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        ui_request = is_ui_api_path(str(scope.get("path") or ""))
        claims = None
        if (
            ui_request
            and not request_authentication_is_bypassed(request)
            and "authorization" not in request.headers
        ):
            try:
                claims = ui_session_claims(request)
            except jwt.PyJWTError:
                response = JSONResponse(
                    {"detail": "Invalid WebUI session"}, status_code=401
                )
                await response(scope, receive, send)
                return
            if claims is not None and not has_valid_ui_csrf(request, claims):
                response = JSONResponse(
                    {"detail": "WebUI CSRF validation failed"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

        if claims is None:
            try:
                claims = verify_request(request)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers or {},
                )
                await response(scope, receive, send)
                return

        claims_token = bind_oauth_claims(claims)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_oauth_claims(claims_token)
