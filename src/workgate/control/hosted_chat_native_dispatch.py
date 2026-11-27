"""Compose the reviewed native adapters; no unrestricted MCP fallback."""

import copy
import json
from collections.abc import Collection
from dataclasses import replace
from typing import Any, Literal

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict

from ..hosted_chat.durable_tasks import (
    SESSION_COMMAND_EFFECT_TOOLS,
    SESSION_FILE_EFFECT_TOOLS,
    SESSION_METADATA_EFFECT_TOOLS,
    SESSION_SHELL_EFFECT_TOOLS,
    DurableTaskRepository,
)
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..protocol.ids import ExecutorId, TaskId
from ..schemas.input_models.session import (
    SessionLabelArg,
    SessionStartWorkdirArg,
)
from ..schemas.input_models.task import TaskLabelArg, TaskObjectiveArg
from .hosted_chat_cancellation import cancel_native_shell, close_native_session
from .hosted_chat_job_dispatch import dispatch_native_job
from .hosted_chat_native_creation import create_native
from .hosted_chat_session_tools import (
    SESSION_FILE_READ_TOOLS,
    SESSION_SHELL_READ_TOOLS,
    NativeSessionDispatcher,
)
from .hosted_chat_task_tools import NativeTaskDispatcher
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


class NativeToolDispatcher:
    """Trusted runner supplies selected tool names and the exact Executor target."""

    def __init__(self, runtime: ControlRuntime) -> None:
        self._runtime = runtime
        self._session = NativeSessionDispatcher(runtime)
        self._task = NativeTaskDispatcher(runtime)

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
        if call.name not in NATIVE_TOOLS or call.name not in allowed_tools:
            raise PermissionError("Native tool is not selected and adapted")
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
