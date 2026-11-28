"""Owner-scoped Agent Builder transport; revisions remain repository-owned."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.agents import AgentRepository
from ...hosted_chat.content_filters import ContentFilterBlocked
from ...hosted_chat.database import SQLiteDatabase
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal
from .hosted_chat_workspace import _query

_PREFIX = UI_API_PREFIX + "/chat/agents"


def hosted_chat_agent_routes(runtime: ControlRuntime) -> list[Route]:
    async def handle(request: Request):
        try:
            payload = (
                await request.json()
                if request.method in {"POST", "PATCH"}
                and request.path_params.get("operation") != "duplicate"
                else None
            )
            principal = _principal(runtime, request)
            principal.check()
            owner = principal.owner
            repository = AgentRepository(
                SQLiteDatabase(runtime.config.state_dir)
            )
            agent_id = request.path_params.get("agent_id")
            operation = request.path_params.get("operation")
            created = False
            changed = False
            if agent_id is None:
                if request.method == "GET":
                    query = _query(request)
                    category = request.query_params.get("category")
                    if category is not None and len(category) > 200:
                        raise ValueError("Invalid category")
                    result = repository.list(owner, category=category, **query)
                else:
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid Agent configuration")
                    result = repository.create(owner, payload)
                    created = True
            elif operation == "versions" and request.method == "GET":
                result = repository.versions(owner, agent_id)
            elif operation == "duplicate" and request.method == "POST":
                result = repository.duplicate(owner, agent_id)
                created = True
            elif operation == "revert" and request.method == "POST":
                if not isinstance(payload, dict) or set(payload) != {
                    "version_index"
                }:
                    raise ValueError("Invalid revision")
                result = repository.revert(
                    owner, agent_id, payload["version_index"]
                )
                changed = True
            elif operation is not None:
                return JSONResponse(
                    {"error": "Agent operation not found"},
                    status_code=404,
                    headers=_HEADERS,
                )
            elif request.method == "GET":
                result = repository.get(owner, agent_id)
            elif request.method == "PATCH":
                if not isinstance(payload, dict):
                    raise ValueError("Invalid Agent configuration")
                result = repository.update(owner, agent_id, payload)
                changed = True
            else:
                result = repository.delete(owner, agent_id)
                changed = True
            if result is None or result is False:
                return JSONResponse(
                    {"error": "Agent not found"},
                    status_code=404,
                    headers=_HEADERS,
                )
            if changed:
                runtime.generation_supervisor.wake_cleanup()
            return JSONResponse(
                {"success": True} if result is True else result,
                status_code=201 if created else 200,
                headers=_HEADERS,
            )
        except ContentFilterBlocked as error:
            return JSONResponse(
                error.response(), status_code=400, headers=_HEADERS
            )
        except PermissionError:
            return JSONResponse(
                {"error": "Agent operation denied"},
                status_code=403,
                headers=_HEADERS,
            )
        except ValueError, KeyError:
            return JSONResponse(
                {"error": "Invalid Agent request"},
                status_code=400,
                headers=_HEADERS,
            )

    return [
        Route(_PREFIX, handle, methods=["GET", "POST"]),
        Route(
            _PREFIX + "/{agent_id}", handle, methods=["GET", "PATCH", "DELETE"]
        ),
        Route(
            _PREFIX + "/{agent_id}/{operation}", handle, methods=["GET", "POST"]
        ),
    ]
