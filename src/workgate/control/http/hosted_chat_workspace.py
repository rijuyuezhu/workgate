"""Owner-scoped native workspace transport; execution writes remain private."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.sampling import validate_sampling
from ...hosted_chat.storage import WorkspaceRepository
from ...ui.http.routes import UI_API_PREFIX
from ..runtime import ControlRuntime
from .hosted_chat import _HEADERS, _principal

_PREFIX = UI_API_PREFIX + "/chat"
type Identifier = Annotated[str, Field(min_length=1, max_length=200)]
type ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class _ConversationChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: ShortText = "New chat"
    projectId: Identifier | None = None
    agent_id: Identifier | None = None
    model: Identifier | None = None
    endpoint: Identifier | None = None
    isArchived: bool = False
    pinned: bool = False
    tags: list[Identifier] = Field(default_factory=list, max_length=100)
    lsmSampling: dict = Field(default_factory=dict)


class _ProjectChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Annotated[str, Field(min_length=1, max_length=160)] = "New project"
    description: Annotated[str, Field(max_length=4000)] = ""
    color: Annotated[str, Field(max_length=80)] | None = None


class _UnreadChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    unread: bool
    expected: Annotated[str, Field(max_length=200)] | None = None


def _query(request: Request) -> dict:
    query = request.query_params
    result = {}
    for key, maximum in (("search", 1000), ("cursor", 4096)):
        value = query.get(key)
        if value is not None:
            if len(value) > maximum:
                raise ValueError("Invalid workspace query")
            result[key] = value
    if "limit" in query:
        limit = int(query["limit"])
        if not 1 <= limit <= 200:
            raise ValueError("Invalid workspace page size")
        result["limit"] = limit
    return result


def _bool(request: Request, key: str) -> bool:
    value = request.query_params.get(key, "false")
    if value not in {"true", "false"}:
        raise ValueError("Invalid boolean filter")
    return value == "true"


def hosted_chat_workspace_routes(runtime: ControlRuntime) -> list[Route]:
    async def repo(request: Request):
        payload = (
            await request.json()
            if request.method in {"POST", "PATCH"}
            else None
        )
        principal = _principal(runtime, request)
        principal.check()
        return (
            principal.owner,
            WorkspaceRepository(SQLiteDatabase(runtime.config.state_dir)),
            payload,
        )

    def response(value, *, created=False):
        if value is None or value is False:
            return JSONResponse(
                {"error": "Workspace resource not found"},
                status_code=404,
                headers=_HEADERS,
            )
        return JSONResponse(
            value if value is not True else {"success": True},
            status_code=201 if created else 200,
            headers=_HEADERS,
        )

    def failure(error):
        status = (
            404
            if isinstance(error, KeyError)
            else 403
            if isinstance(error, PermissionError)
            else 400
        )
        return JSONResponse(
            {
                "error": {
                    400: "Invalid workspace request",
                    403: "Workspace operation denied",
                    404: "Workspace resource not found",
                }[status]
            },
            status_code=status,
            headers=_HEADERS,
        )

    async def conversations(request: Request):
        try:
            owner, workspace, payload = await repo(request)
            if request.method == "POST":
                changes = _ConversationChanges.model_validate(
                    payload
                ).model_dump(exclude_unset=True)
                if "lsmSampling" in changes:
                    changes["lsmSampling"] = validate_sampling(
                        changes["lsmSampling"]
                    )
                return response(
                    workspace.create_conversation(owner, changes), created=True
                )
            filters = _query(request)
            filters["archived"] = _bool(request, "archived")
            filters["pinned"] = _bool(request, "pinned")
            project = request.query_params.get("projectId")
            tags = request.query_params.getlist("tag")
            if (
                (project is not None and len(project) > 200)
                or len(tags) > 100
                or any(not tag or len(tag) > 200 for tag in tags)
            ):
                raise ValueError("Invalid workspace filter")
            return response(
                workspace.conversations(
                    owner, project_id=project, tags=tags, **filters
                )
            )
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def conversation(request: Request):
        try:
            owner, workspace, payload = await repo(request)
            cid = request.path_params["conversation_id"]
            if request.method == "GET":
                return response(workspace.conversation(owner, cid))
            if request.method == "DELETE":
                value = workspace.delete_conversation(
                    owner, cid, require_idle=True
                )
                if value:
                    runtime.generation_supervisor.wake_cleanup()
                return response(value)
            changes = _ConversationChanges.model_validate(payload).model_dump(
                exclude_unset=True
            )
            if "lsmSampling" in changes:
                changes["lsmSampling"] = validate_sampling(
                    changes["lsmSampling"]
                )
            result = workspace.update_conversation(owner, cid, changes)
            if result is not None and (
                "projectId" in changes or "agent_id" in changes
            ):
                runtime.generation_supervisor.wake_cleanup()
            return response(result)
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def messages(request: Request):
        try:
            owner, workspace, payload = await repo(request)
            query = _query(request)
            query.pop("search", None)
            return response(
                workspace.messages(
                    owner, request.path_params["conversation_id"], **query
                )
            )
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def search(request: Request):
        try:
            owner, workspace, payload = await repo(request)
            query = _query(request)
            return response(
                {
                    "messages": workspace.search_messages(
                        owner,
                        query.get("search", ""),
                        limit=query.get("limit", 30),
                    )
                }
            )
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def projects(request: Request):
        try:
            owner, workspace, payload = await repo(request)
            if request.method == "POST":
                changes = _ProjectChanges.model_validate(payload).model_dump(
                    exclude_unset=True
                )
                if "name" not in changes:
                    raise ValueError("Project name required")
                return response(
                    workspace.create_project(owner, **changes), created=True
                )
            query = _query(request)
            query.pop("cursor", None)
            return response(workspace.projects(owner, **query))
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def project(request: Request):
        try:
            owner, workspace, payload = await repo(request)
            pid = request.path_params["project_id"]
            if request.method == "GET":
                return response(workspace.project(owner, pid))
            if request.method == "DELETE":
                value = workspace.delete_project(owner, pid)
                if value:
                    runtime.generation_supervisor.wake_cleanup()
                return response(value)
            changes = _ProjectChanges.model_validate(payload).model_dump(
                exclude_unset=True
            )
            return response(workspace.update_project(owner, pid, changes))
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def unread(request: Request):
        try:
            owner, workspace, payload = await repo(request)
            if request.method == "GET":
                return response({"unread": workspace.unread(owner)})
            cid = request.path_params["conversation_id"]
            change = _UnreadChange.model_validate(payload)
            return response(
                {
                    "conversationId": cid,
                    "marker": workspace.set_unread(
                        owner,
                        cid,
                        unread=change.unread,
                        expected=change.expected,
                    ),
                }
            )
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    return [
        Route(
            _PREFIX + "/conversations", conversations, methods=["GET", "POST"]
        ),
        Route(_PREFIX + "/conversations/unread", unread, methods=["GET"]),
        Route(
            _PREFIX + "/conversations/{conversation_id}",
            conversation,
            methods=["GET", "PATCH", "DELETE"],
        ),
        Route(
            _PREFIX + "/conversations/{conversation_id}/messages",
            messages,
            methods=["GET"],
        ),
        Route(
            _PREFIX + "/conversations/{conversation_id}/unread",
            unread,
            methods=["POST"],
        ),
        Route(_PREFIX + "/messages/search", search, methods=["GET"]),
        Route(_PREFIX + "/projects", projects, methods=["GET", "POST"]),
        Route(
            _PREFIX + "/projects/{project_id}",
            project,
            methods=["GET", "PATCH", "DELETE"],
        ),
    ]
