"""Native authenticated artifact editing endpoint."""

import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.artifact_edits import ArtifactConflict, update_artifact
from ...hosted_chat.content_filters import ContentFilterBlocked
from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.machine_permissions import require_machine_scope
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal


def hosted_chat_artifact_routes(runtime: ControlRuntime) -> list[Route]:
    async def edit(request: Request):
        try:
            principal = _principal(runtime, request)

            def check():
                principal.check()
                require_machine_scope(
                    set(principal.scopes), "write", expires=principal.expires
                )

            check()
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {
                "index",
                "original",
                "updated",
            }:
                raise ValueError("Invalid artifact edit")
            result = update_artifact(
                SQLiteDatabase(runtime.config.state_dir),
                principal.owner,
                request.path_params["message_id"],
                payload["index"],
                payload["original"],
                payload["updated"],
                check_grant=check,
            )
            return JSONResponse(result, headers=_HEADERS)
        except ContentFilterBlocked as error:
            return JSONResponse(
                error.response(), status_code=400, headers=_HEADERS
            )
        except ArtifactConflict:
            return JSONResponse(
                {"error": "Artifact history cannot be edited"},
                status_code=409,
                headers=_HEADERS,
            )
        except KeyError:
            return JSONResponse(
                {"error": "Message not found"},
                status_code=404,
                headers=_HEADERS,
            )
        except PermissionError:
            return JSONResponse(
                {"error": "Artifact edit denied"},
                status_code=403,
                headers=_HEADERS,
            )
        except ValueError, TypeError:
            return JSONResponse(
                {"error": "Invalid artifact edit"},
                status_code=400,
                headers=_HEADERS,
            )
        except sqlite3.Error:
            return JSONResponse(
                {"error": "Artifact edit failed"},
                status_code=500,
                headers=_HEADERS,
            )

    return [
        Route(
            UI_API_PREFIX + "/chat/messages/artifact/{message_id}",
            edit,
            methods=["POST"],
        )
    ]
