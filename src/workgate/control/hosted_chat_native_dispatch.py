"""Compose the reviewed native adapters; no unrestricted MCP fallback."""

import copy
import json
from collections.abc import Collection
from dataclasses import replace
from typing import Any, Literal

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, TypeAdapter

from ..hosted_chat.durable_tasks import (
    SESSION_COMMAND_EFFECT_TOOLS,
    SESSION_FILE_EFFECT_TOOLS,
    SESSION_METADATA_EFFECT_TOOLS,
    SESSION_SHELL_EFFECT_TOOLS,
    DurableTaskRepository,
    ReservedCopyRequest,
)
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.machine_permissions import (
    agent_access_policy,
    effective_machine_access,
    require_machine_scope,
)
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..hosted_chat.storage import WorkspaceRepository
from ..protocol.ids import ExecutorId, TaskId
from ..schemas.input_models.session import (
    SessionLabelArg,
    SessionStartWorkdirArg,
)
from ..schemas.input_models.task import TaskLabelArg, TaskObjectiveArg
from .hosted_chat_cancellation import cancel_native_shell, close_native_session
from .hosted_chat_job_dispatch import dispatch_native_job
from .hosted_chat_native_creation import _grant, _registered, create_native
from .hosted_chat_session_tools import (
    SESSION_FILE_READ_TOOLS,
    SESSION_SHELL_READ_TOOLS,
    NativeSessionDispatcher,
)
from .hosted_chat_task_tools import NativeTaskDispatcher
from .mcp.app import build_mcp
from .runtime import ControlRuntime

TASK_TOOLS = frozenset({"task", "task_plan", "read_todos", "write_todos"})
SESSION_TOOLS = (
    SESSION_FILE_READ_TOOLS
    | SESSION_SHELL_READ_TOOLS
    | SESSION_FILE_EFFECT_TOOLS
    | SESSION_SHELL_EFFECT_TOOLS
    | SESSION_METADATA_EFFECT_TOOLS
    | SESSION_COMMAND_EFFECT_TOOLS
    | {
        "job",
        "kill_persistent_shell",
        "session_end",
        "session_start",
        "session_copy",
    }
)
NATIVE_TOOLS = TASK_TOOLS | SESSION_TOOLS


class _CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["create"]
    label: TaskLabelArg = None
    objective: TaskObjectiveArg = None


class _StartSession(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    executor_id: ExecutorId
    task_id: TaskId
    workdir: SessionStartWorkdirArg = None
    label: SessionLabelArg = None


def _envelope(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize native creation/inspection receipts to the MCP envelope."""
    return CallToolResult(
        structured_content=result,
        content=[
            TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, allow_nan=False),
            )
        ],
    ).model_dump(mode="json", by_alias=True, exclude_none=True)


def _selected_tools(
    agent: dict | None, allowed: Collection[str]
) -> frozenset[str]:
    selected = NATIVE_TOOLS.intersection(allowed)
    if agent is not None and agent_access_policy(agent) is None:
        # Pinned legacy Agent selection, with the native equivalents of the
        # always-present logical Session and plan management tools.
        selected &= frozenset(agent.get("tools") or ()) | {"task", "task_plan"}
    return selected


def _scope_allows(
    scopes: set[str], operation: str, expires: int | None
) -> bool:
    try:
        require_machine_scope(scopes, operation, expires=expires)
    except PermissionError:
        return False
    return True


class NativeToolDispatcher:
    """Trusted runner supplies selected tool names and the exact Executor target."""

    def __init__(self, runtime: ControlRuntime) -> None:
        self._runtime = runtime
        self._mcp = build_mcp(runtime=runtime, audit_transport="hosted-chat")
        self._session = NativeSessionDispatcher(runtime, mcp=self._mcp)
        self._task = NativeTaskDispatcher(runtime, mcp=self._mcp)

    async def definitions(
        self,
        repository: DurableTaskRepository,
        lease: ActorLease,
        *,
        allowed_tools: Collection[str],
        scopes: set[str],
        expires: int | None,
        task_machine: str | None = None,
    ) -> list[dict]:
        """Advertise only the canonical, adapted surface for the live grant."""
        runtime = self._runtime
        if repository.database.root != runtime.config.state_dir / "hosted-chat":
            raise PermissionError(
                "Native intent belongs to another Control runtime"
            )
        tools = await self._mcp.list_tools()
        scopes, expires = _grant(runtime, lease.owner, scopes, expires)
        conversation_id, agent = repository.tool_context(lease)
        selected = _selected_tools(agent, allowed_tools)
        policy, _ = effective_machine_access(
            lease.owner,
            conversation_id,
            agent=agent,
            scopes=scopes,
            expires=expires,
            repo=WorkspaceRepository(repository.database),
            registered=_registered(runtime),
        )
        readable = [
            key for key, level in policy.items() if level in {"read", "write"}
        ]
        writable = any(level == "write" for level in policy.values())
        execute = writable and _scope_allows(scopes, "execute", expires)
        task_write = _scope_allows(scopes, "write", expires)
        task_read = _scope_allows(scopes, "read", expires)
        read_tools = (
            SESSION_FILE_READ_TOOLS
            | SESSION_SHELL_READ_TOOLS
            | SESSION_METADATA_EFFECT_TOOLS
            | {"session_start", "job"}
        )
        write_tools = SESSION_FILE_EFFECT_TOOLS | {"session_copy"}
        execute_tools = (
            SESSION_COMMAND_EFFECT_TOOLS
            | SESSION_SHELL_EFFECT_TOOLS
            | {"kill_persistent_shell", "session_end"}
        )
        available = set()
        if readable:
            available.update(read_tools)
        if writable:
            available.update(write_tools)
        if execute:
            available.update(execute_tools)
        if task_write:
            available.update(TASK_TOOLS - {"read_todos"})
        if task_read:
            available.add("read_todos")

        definitions = []
        for tool in tools:
            if tool.name not in selected or tool.name not in available:
                continue
            schema = copy.deepcopy(tool.input_schema)
            schema["additionalProperties"] = False
            properties = schema["properties"]
            description = tool.description or ""
            if tool.name == "task":
                if task_machine not in readable:
                    properties["action"]["enum"].remove("create")
                else:
                    description += " Create requires the explicitly selected Executor; other actions require an owned Task."
            elif tool.name == "session_start":
                schema = _StartSession.model_json_schema()
                schema["properties"]["executor_id"]["enum"] = readable
                description += " Supply an authorized executor_id and owned task_id explicitly."
            elif tool.name == "session_end":
                properties.pop("force", None)
                description = "End an owned execution Session after positive native absence confirmation; attached Task remains."
            elif tool.name == "session_copy":
                properties.pop("background", None)
                description = "Start a durable managed copy between owned Sessions with readable source and writable destination."
            elif tool.name == "job":
                if not writable:
                    for key in ("cancel", "retry"):
                        properties.pop(key, None)
                    description = (
                        "List or poll exact owned native Job attempts."
                    )
                else:
                    description += " Mutating tracked Jobs requires execute; managed copies require source read and destination write."
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": description[:2048],
                        "parameters": schema,
                    },
                }
            )
        return definitions

    def prepare_model_batch(
        self,
        repository: DurableTaskRepository,
        lease: ActorLease,
        calls: list[ModelToolCall],
        *,
        allowed_tools: Collection[str],
        task_machine: str | None = None,
    ) -> list[tuple[ModelToolCall, str | None]]:
        """Resolve every target before any sibling is dispatched.

        This is not an authorization grant: each adapter checks current
        native permissions again immediately before its own effect.
        """
        runtime = self._runtime
        if repository.database.root != runtime.config.state_dir / "hosted-chat":
            raise PermissionError(
                "Native intent belongs to another Control runtime"
            )
        calls = copy.deepcopy(complete_tool_batch(calls))
        conversation_id, agent = repository.tool_context(lease)
        selected = _selected_tools(agent, allowed_tools)
        if any(call.name not in selected for call in calls):
            raise PermissionError("Native tool is not selected and adapted")
        prepared = []
        for call in calls:
            machine = None
            if call.name in TASK_TOOLS:
                if (
                    call.name == "task"
                    and call.arguments.get("action") == "create"
                ):
                    _CreateTask.model_validate(call.arguments)
                    if task_machine is None:
                        raise PermissionError(
                            "Native Task creation requires an explicit Executor"
                        )
                    machine = TypeAdapter(ExecutorId).validate_python(
                        task_machine, strict=True
                    )
            elif call.name == "session_start":
                request = _StartSession.model_validate(call.arguments)
                if not repository.owned(
                    lease.owner, conversation_id, "task", request.task_id
                ):
                    raise PermissionError(
                        "Native Task does not belong to this conversation"
                    )
                machine = request.executor_id
            elif call.name == "session_copy":
                copy_request = ReservedCopyRequest.model_validate(
                    call.arguments
                )
                machine = repository.resolve_session_executor(
                    lease, copy_request.src_session_id
                )
                repository.resolve_session_executor(
                    lease, copy_request.dst_session_id
                )
            else:
                machine = repository.resolve_session_executor(
                    lease,
                    call.arguments.get("session_id"),
                    closing=call.name == "session_end",
                )
            prepared.append((call, machine))
        return prepared

    async def dispatch_model_call(
        self,
        repository: DurableTaskRepository,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        allowed_tools: Collection[str],
        scopes: set[str],
        expires: int | None,
        task_machine: str | None = None,
    ) -> dict[str, Any]:
        [(prepared, machine)] = self.prepare_model_batch(
            repository,
            lease,
            [call],
            allowed_tools=allowed_tools,
            task_machine=task_machine,
        )
        return await self.dispatch(
            repository,
            lease,
            prepared,
            machine=machine,
            allowed_tools=allowed_tools,
            scopes=scopes,
            expires=expires,
        )

    async def dispatch(
        self,
        repository: DurableTaskRepository,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        machine: str | None,
        allowed_tools: Collection[str],
        scopes: set[str],
        expires: int | None,
    ) -> dict[str, Any]:
        runtime = self._runtime
        call = copy.deepcopy(call)
        complete_tool_batch([call])
        if repository.database.root != runtime.config.state_dir / "hosted-chat":
            raise PermissionError(
                "Native intent belongs to another Control runtime"
            )
        if call.name not in NATIVE_TOOLS or call.name not in allowed_tools:
            raise PermissionError("Native tool is not selected and adapted")
        _, agent = repository.tool_context(lease)
        if call.name not in _selected_tools(agent, allowed_tools):
            raise PermissionError(
                "Native tool is not selected by the live Agent"
            )
        if call.name in TASK_TOOLS:
            if call.name == "task" and call.arguments.get("action") == "create":
                if machine is None:
                    raise PermissionError(
                        "Native Task creation requires an explicit Executor"
                    )
                request = _CreateTask.model_validate(call.arguments)
                return _envelope(
                    await create_native(
                        runtime,
                        repository,
                        lease,
                        replace(
                            call, arguments=request.model_dump(mode="json")
                        ),
                        machine=machine,
                        scopes=scopes,
                        expires=expires,
                    )
                )
            if machine is not None:
                raise ValueError("Native Task metadata tools are Control-owned")
            return await self._task.dispatch(
                repository,
                lease,
                call,
                scopes=scopes,
                expires=expires,
            )
        if machine is None:
            raise PermissionError(
                "Native Session tools require an explicit Executor"
            )
        if call.name == "session_start":
            request = _StartSession.model_validate(call.arguments)
            call = replace(call, arguments=request.model_dump(mode="json"))
            execute = create_native
        elif call.name == "session_copy":
            execute = create_native
        elif call.name == "job":
            execute = dispatch_native_job
        elif call.name == "kill_persistent_shell":
            execute = cancel_native_shell
        elif call.name == "session_end":
            execute = close_native_session
        elif call.name in SESSION_COMMAND_EFFECT_TOOLS:
            flags = [
                call.arguments.get(flag, False)
                for flag in ("async_", "pty", "persist_on_timeout")
            ]
            if any(type(flag) is not bool for flag in flags):
                raise ValueError("Native execution mode flags must be boolean")
            if flags[1] and (flags[0] or flags[2]):
                raise ValueError(
                    "Tracked and PTY execution modes are mutually exclusive"
                )
            if any(flags):
                execute = create_native
            else:
                return await self._session.dispatch(
                    repository,
                    lease,
                    call,
                    machine=machine,
                    scopes=scopes,
                    expires=expires,
                )
        else:
            return await self._session.dispatch(
                repository,
                lease,
                call,
                machine=machine,
                scopes=scopes,
                expires=expires,
            )
        return _envelope(
            await execute(
                runtime,
                repository,
                lease,
                call,
                machine=machine,
                scopes=scopes,
                expires=expires,
            )
        )
