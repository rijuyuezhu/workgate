"""Authenticated owner-scoped Action authoring; no execution endpoint."""

import asyncio
import json
import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.actions import ActionRepository
from ...hosted_chat.content_filters import ContentFilterBlocked
from ...hosted_chat.credential_cipher import CredentialUnavailable
from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.machine_permissions import require_machine_scope
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat/agents/actions"
_MAX_BODY = 1024 * 1024


async def _body(request: Request, check) -> dict:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ValueError("JSON required")
    data = bytearray()
    async for chunk in request.stream():
        check()
        if len(data) + len(chunk) > _MAX_BODY:
            raise ValueError("Action request exceeds size limit")
        data.extend(chunk)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Invalid Action request")
    return value


def hosted_chat_action_routes(runtime: ControlRuntime) -> list[Route]:
    async def handle(request: Request):
        try:
            principal = _principal(runtime, request)

            def check():
                principal.check()
                require_machine_scope(
                    set(principal.scopes),
                    "read" if request.method == "GET" else "write",
                    expires=principal.expires,
                )

            check()
            owner = principal.owner
            repository = ActionRepository(
                SQLiteDatabase(runtime.config.state_dir)
            )
            if request.method == "GET":
                result = await asyncio.to_thread(repository.list, owner)
                check()
            elif request.method == "POST":
                body = await _body(request, check)
                result = await asyncio.to_thread(
                    repository.save,
                    owner,
                    request.path_params["agent_id"],
                    body,
                    check_grant=check,
                )
            else:
                deleted = await asyncio.to_thread(
                    repository.delete,
                    owner,
                    request.path_params["agent_id"],
                    request.path_params["action_id"],
                    check_grant=check,
                )
                if not deleted:
                    raise KeyError("Action not found")
                result = {"message": "Action deleted successfully"}
            return JSONResponse(result, headers=_HEADERS)
        except PermissionError:
            return JSONResponse(
                {"error": "Action operation denied"},
                status_code=403,
                headers=_HEADERS,
            )
        except KeyError:
            return JSONResponse(
                {"error": "Action or Agent not found"},
                status_code=404,
                headers=_HEADERS,
            )
        except CredentialUnavailable:
            return JSONResponse(
                {"error": "Action credentials unavailable"},
                status_code=409,
                headers=_HEADERS,
            )
        except ContentFilterBlocked as error:
            return JSONResponse(
                error.response(), status_code=400, headers=_HEADERS
            )
        except ValueError, TypeError, OverflowError, RecursionError:
            return JSONResponse(
                {"error": "Invalid Action request"},
                status_code=400,
                headers=_HEADERS,
            )
        except OSError, sqlite3.Error:
            return JSONResponse(
                {"error": "Action storage unavailable"},
                status_code=503,
                headers=_HEADERS,
            )

    return [
        Route(_PREFIX, handle, methods=["GET"]),
        Route(_PREFIX + "/{agent_id}", handle, methods=["POST"]),
        Route(_PREFIX + "/{agent_id}/{action_id}", handle, methods=["DELETE"]),
    ]
