"""Owner-scoped manual memory management without model execution."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.memory import MemoryRepository, memory_config
from ...hosted_chat.memory_protection import MemoryContentBlocked
from ...hosted_chat.storage import WorkspaceRepository
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat/memories"


def hosted_chat_memory_routes(runtime: ControlRuntime) -> list[Route]:
    async def handle(request: Request, *, preferences: bool = False):
        try:
            payload = (
                await request.json()
                if request.method in {"POST", "PATCH"}
                else None
            )
            principal = _principal(runtime, request)
            principal.check()
            owner = principal.owner
            database = SQLiteDatabase(runtime.config.state_dir)
            repo = WorkspaceRepository(database)
            store = MemoryRepository(database)
            config = memory_config(repo, owner)
            if preferences:
                if request.method == "PATCH":
                    if config is None:
                        raise PermissionError("Memory is disabled")
                    if (
                        not isinstance(payload, dict)
                        or set(payload) != {"memories"}
                        or type(payload["memories"]) is not bool
                    ):
                        raise ValueError("Invalid memory preference")
                    repo.save_setting(
                        owner, "user.memories", payload["memories"]
                    )
                return JSONResponse(
                    {
                        "available": config is not None,
                        "memories": repo.setting(owner, "user.memories", True)
                        is not False,
                    },
                    headers=_HEADERS,
                )
            if config is None and request.method != "DELETE":
                raise PermissionError("Memory is disabled")
            identifier = request.path_params.get("entry_id")
            key = request.path_params.get("key")
            agent_id = request.query_params.get("agentId", "")
            if len(agent_id) > 128:
                raise ValueError("Invalid Agent partition")
            if request.method == "GET":
                assert config is not None
                memories = store.list(owner)
                total = sum(
                    item["tokenCount"]
                    for item in memories
                    if not item.get("agentId")
                )
                limit = config.get("tokenLimit") or None
                return JSONResponse(
                    {
                        "memories": memories,
                        "totalTokens": total,
                        "tokenLimit": limit,
                        "charLimit": config.get("charLimit") or 10000,
                        "usagePercentage": min(
                            100, int(total / limit * 100 + 0.5)
                        )
                        if type(limit) is int and limit > 0
                        else None,
                    },
                    headers=_HEADERS,
                )
            if request.method == "DELETE":
                if not store.delete(
                    owner, entry_id=identifier, key=key, agent_id=agent_id
                ):
                    raise KeyError("Memory not found")
                return JSONResponse({"deleted": True}, headers=_HEADERS)
            if not isinstance(payload, dict) or not set(payload) <= {
                "key",
                "value",
                "agentId",
            }:
                raise ValueError("Invalid memory request")
            created = request.method == "POST"
            if created:
                agent_id = payload.get("agentId", "")
            elif "agentId" in payload:
                raise ValueError("Cannot change memory partition")
            if not isinstance(agent_id, str) or len(agent_id) > 128:
                raise ValueError("Invalid Agent partition")
            new_key = payload.get("key")
            value = payload.get("value")
            if (
                new_key is not None and not isinstance(new_key, str)
            ) or not isinstance(value, str):
                raise ValueError("Invalid memory value")
            if created and new_key is None:
                raise ValueError("Memory key required")
            record = store.write(
                owner,
                new_key.strip() if new_key is not None else None,
                value.strip(),
                agent_id=agent_id,
                create=created,
                existing_id=identifier,
                existing_key=key,
            )
            return JSONResponse(
                {"created" if created else "updated": True, "memory": record},
                status_code=201 if created else 200,
                headers=_HEADERS,
            )
        except MemoryContentBlocked as error:
            return JSONResponse(
                error.response(), status_code=400, headers=_HEADERS
            )
        except FileExistsError:
            return JSONResponse(
                {"error": "Memory key already exists"},
                status_code=409,
                headers=_HEADERS,
            )
        except KeyError:
            return JSONResponse(
                {"error": "Memory resource not found"},
                status_code=404,
                headers=_HEADERS,
            )
        except PermissionError:
            return JSONResponse(
                {"error": "Memory operation denied"},
                status_code=403,
                headers=_HEADERS,
            )
        except ValueError, OverflowError:
            return JSONResponse(
                {"error": "Invalid memory request or quota exceeded"},
                status_code=400,
                headers=_HEADERS,
            )

    async def preference(request: Request):
        return await handle(request, preferences=True)

    return [
        Route(_PREFIX, handle, methods=["GET", "POST"]),
        Route(_PREFIX + "/preferences", preference, methods=["GET", "PATCH"]),
        Route(_PREFIX + "/id/{entry_id}", handle, methods=["PATCH", "DELETE"]),
        Route(_PREFIX + "/{key}", handle, methods=["PATCH", "DELETE"]),
    ]
