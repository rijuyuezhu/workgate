"""Native Hosted Chat policy preferences; never an execution grant."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.agents import AgentRepository
from ...hosted_chat.approval_settings import (
    CHAT_PREFIX,
    GLOBAL_KEY,
    effective_mode,
    save_mode,
)
from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.generations import GenerationStore
from ...hosted_chat.machine_permissions import (
    effective_machine_access,
    machine_policy,
    require_machine_scope,
    validate_access_map,
)
from ...hosted_chat.storage import WorkspaceRepository
from ...ui.http.routes import UI_API_PREFIX
from ..hosted_chat_native_creation import _registered
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat"


def hosted_chat_policy_routes(runtime: ControlRuntime) -> list[Route]:
    async def handle(request: Request):
        try:
            payload = await request.json() if request.method == "PUT" else None
            principal = _principal(runtime, request)
            principal.check()
            owner = principal.owner
            scopes = set(principal.scopes)
            database = SQLiteDatabase(runtime.config.state_dir)
            repo = WorkspaceRepository(database)
            cid = request.path_params.get("conversation_id")
            pid = request.path_params.get("project_id")
            operation = request.path_params["operation"]
            registered = _registered(runtime)
            agent = None
            if cid is not None:
                conversation = repo.conversation(owner, cid)
                if conversation is None:
                    raise KeyError("Conversation not found")
                latest = GenerationStore(database).latest(owner, cid)
                agent_id = (
                    latest.agent_id if latest else conversation.get("agent_id")
                )
                if agent_id:
                    agent = AgentRepository(database).get(owner, agent_id)
                    if agent is None:
                        raise PermissionError("Agent is unavailable")
            if operation == "approval" and pid is None:
                if request.method == "PUT":
                    require_machine_scope(
                        scopes, "write", expires=principal.expires
                    )
                    if not isinstance(payload, dict) or set(payload) != {
                        "mode"
                    }:
                        raise ValueError("Invalid approval mode")
                    save_mode(repo, owner, payload["mode"], conversation_id=cid)
                key = CHAT_PREFIX + cid if cid is not None else GLOBAL_KEY
                mode = repo.setting(
                    owner, key, "inherit" if cid is not None else "automatic"
                )
                result = {
                    "mode": mode,
                    "effective": effective_mode(repo, owner, cid, agent)
                    if cid is not None
                    else mode,
                }
            elif operation == "permissions":
                changes = None
                if request.method == "PUT":
                    require_machine_scope(
                        scopes, "write", expires=principal.expires
                    )
                    if not isinstance(payload, dict) or set(payload) != {
                        "machines"
                    }:
                        raise ValueError("Invalid machine policy")
                    changes = validate_access_map(payload["machines"])
                result = machine_policy(
                    owner,
                    repo=repo,
                    registered=registered,
                    conversation_id=cid,
                    project_id=pid,
                    changes=changes,
                )
                if changes is not None:
                    runtime.generation_supervisor.wake_cleanup()
                if cid is not None:
                    result["authorized"], result["agentDefault"] = (
                        effective_machine_access(
                            owner,
                            cid,
                            agent=agent,
                            scopes=scopes,
                            expires=principal.expires,
                            repo=repo,
                            registered=registered,
                        )
                    )
            else:
                raise KeyError("Unknown policy operation")
            return JSONResponse(result, headers=_HEADERS)
        except KeyError:
            return JSONResponse(
                {"error": "Policy resource not found"},
                status_code=404,
                headers=_HEADERS,
            )
        except PermissionError:
            return JSONResponse(
                {"error": "Policy operation denied"},
                status_code=403,
                headers=_HEADERS,
            )
        except ValueError:
            return JSONResponse(
                {"error": "Invalid policy request"},
                status_code=400,
                headers=_HEADERS,
            )

    return [
        Route(
            _PREFIX + "/settings/{operation}", handle, methods=["GET", "PUT"]
        ),
        Route(
            _PREFIX + "/projects/{project_id}/{operation}",
            handle,
            methods=["GET", "PUT"],
        ),
        Route(
            _PREFIX + "/conversations/{conversation_id}/{operation}",
            handle,
            methods=["GET", "PUT"],
        ),
    ]
