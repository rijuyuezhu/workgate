"""Protected Action OAuth start and one-use browser callback."""

import asyncio
import hashlib
import sqlite3
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import BaseRoute, Route

from ...hosted_chat.action_oauth_flow import (
    _CALLBACK,
    ActionOAuthFlows,
    callback_uri,
)
from ...hosted_chat.actions import ActionRepository
from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.machine_permissions import require_machine_scope
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal
from .hosted_chat_actions import _body

_START = "/api/ui/chat/actions/oauth/start"
_CALLBACK_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
}


def _cookie_name(state: str) -> str:
    return "wg_action_oauth_" + hashlib.sha256(state.encode()).hexdigest()[:24]


def hosted_chat_action_oauth_routes(
    runtime: ControlRuntime,
) -> tuple[list[BaseRoute], list[BaseRoute]]:
    def flow():
        return ActionOAuthFlows(
            ActionRepository(SQLiteDatabase(runtime.config.state_dir))
        )

    def origin(request: Request) -> str:
        base = runtime.config.base_url
        if not base:
            raise ValueError("Configure Control base_url")
        redirect = callback_uri(base)
        if (
            request.headers.get("host", "").lower()
            != urlsplit(base).netloc.lower()
        ):
            raise ValueError("Invalid Control callback host")
        return redirect

    async def start(request: Request):
        try:
            redirect = origin(request)
            principal = _principal(runtime, request)

            def check():
                principal.check()
                require_machine_scope(
                    set(principal.scopes), "write", expires=principal.expires
                )

            check()
            body = await _body(request, check)
            if set(body) != {"runId", "actionId"} or any(
                not isinstance(body[key], str) or not 1 <= len(body[key]) <= 128
                for key in ("runId", "actionId")
            ):
                raise ValueError("Invalid OAuth selection")
            base = runtime.config.base_url
            assert base is not None
            result = await asyncio.to_thread(
                flow().begin,
                principal.owner,
                body["runId"],
                body["actionId"],
                base,
                check_grant=check,
            )
            state = parse_qs(urlsplit(result.authorization_url).query)["state"][
                0
            ]
            response = JSONResponse(
                {
                    "authorizationUrl": result.authorization_url,
                    "expiresAt": result.expires_at * 1000,
                },
                headers=_HEADERS,
            )
            response.set_cookie(
                _cookie_name(state),
                result.cookie,
                max_age=600,
                path=_CALLBACK,
                httponly=True,
                secure=urlsplit(redirect).scheme == "https",
                samesite="lax",
            )
            return response
        except PermissionError:
            return JSONResponse(
                {"error": "OAuth authorization denied"},
                status_code=403,
                headers=_HEADERS,
            )
        except KeyError:
            return JSONResponse(
                {"error": "Pending authorization not found"},
                status_code=404,
                headers=_HEADERS,
            )
        except ValueError, TypeError, RecursionError:
            return JSONResponse(
                {"error": "Invalid OAuth authorization request"},
                status_code=400,
                headers=_HEADERS,
            )
        except OSError, sqlite3.Error:
            return JSONResponse(
                {"error": "OAuth authorization unavailable"},
                status_code=503,
                headers=_HEADERS,
            )

    async def callback(request: Request):
        name = None
        success = False
        try:
            redirect = origin(request)
            if len(request.scope.get("query_string", b"")) > 8192:
                raise ValueError("Invalid OAuth callback")
            query = request.query_params
            states = query.getlist("state")
            codes = query.getlist("code")
            if len(states) != 1 or not 20 <= len(states[0]) <= 256:
                raise ValueError("Invalid OAuth callback")
            name = _cookie_name(states[0])
            cookie = request.cookies.get(name, "")
            repository = flow()
            claim = await asyncio.to_thread(
                repository.claim, states[0], cookie, redirect
            )
            if len(codes) != 1 or query.getlist("error"):
                raise ValueError("OAuth authorization not completed")
            await repository.complete(claim, codes[0])
            success = True
        except (
            KeyError,
            ValueError,
            PermissionError,
            OSError,
            sqlite3.Error,
            TypeError,
        ):
            pass
        response = HTMLResponse(
            "Authorization completed. Return to WorkGate and resume the saved generation."
            if success
            else "Authorization was not completed. Return to WorkGate to retry.",
            status_code=200 if success else 400,
            headers=_CALLBACK_HEADERS,
        )
        if name is not None:
            response.delete_cookie(
                name,
                path=_CALLBACK,
                httponly=True,
                samesite="lax",
                secure=bool(
                    runtime.config.base_url
                    and urlsplit(runtime.config.base_url).scheme == "https"
                ),
            )
        return response

    return [Route(_START, start, methods=["POST"])], [
        Route(_CALLBACK, callback, methods=["GET"])
    ]
