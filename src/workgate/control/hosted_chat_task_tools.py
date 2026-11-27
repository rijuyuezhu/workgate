"""Control-owned Task metadata access for Hosted Chat."""

import json
from dataclasses import replace
from typing import Literal, Self

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, model_validator

from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..protocol.ids import TaskId
from ..schemas.input_models.task import (
    PlanStepsArg,
    ProgressBlockersArg,
    ProgressFindingsArg,
    ProgressNextActionArg,
    ProgressSummaryArg,
    TaskLabelArg,
    TaskObjectiveArg,
)
from ..schemas.input_models.todo import TodosArg
from ..schemas.result_models.task import TaskDeleteOutput, TaskOutput
from ..schemas.result_models.todo import ReadTodosOutput
from .hosted_chat_native_creation import NativeCreationUncertain, _grant
from .hosted_chat_session_tools import _await_authorized
from .mcp.app import build_mcp
from .runtime import ControlRuntime


class _TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: TaskId


class _TaskMetadata(_TaskRequest):
    action: Literal[
        "get", "report", "block", "resume", "finish", "cancel", "delete"
    ]
    label: TaskLabelArg = None
    objective: TaskObjectiveArg = None
    summary: ProgressSummaryArg = None
    findings: ProgressFindingsArg = None
    next_action: ProgressNextActionArg = None
    blockers: ProgressBlockersArg = None

    @model_validator(mode="after")
    def report_fields(self) -> Self:
        if self.action != "report" and any(
            value is not None
            for value in (
                self.label,
                self.objective,
                self.summary,
                self.findings,
                self.next_action,
                self.blockers,
            )
        ):
            raise ValueError("Only report accepts Task progress fields")
        return self


class _TaskPlan(_TaskRequest):
    steps: PlanStepsArg


class _WriteTodos(_TaskRequest):
    todos: TodosArg


_REQUESTS = {
    "task": _TaskMetadata,
    "task_plan": _TaskPlan,
    "read_todos": _TaskRequest,
    "write_todos": _WriteTodos,
}


class NativeTaskDispatcher:
    """Use the native Task API, without implicit machine access."""

    def __init__(self, runtime: ControlRuntime) -> None:
        self._runtime = runtime
        self._mcp = build_mcp(runtime=runtime, audit_transport="hosted-chat")

    async def dispatch(
        self,
        repository: DurableTaskRepository,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        scopes: set[str],
        expires: int | None,
    ) -> dict:
        runtime = self._runtime
        if repository.database.root != runtime.config.state_dir / "hosted-chat":
            raise PermissionError(
                "Native intent belongs to another Control runtime"
            )
        scopes, expires = _grant(runtime, lease.owner, scopes, expires)
        complete_tool_batch([call])
        if call.name not in _REQUESTS:
            raise PermissionError("Not an authorized native Task metadata tool")
        request = _REQUESTS[call.name].model_validate(call.arguments)
        call = replace(call, arguments=request.model_dump(mode="json"))
        mutation = call.name != "read_todos" and not (
            isinstance(request, _TaskMetadata) and request.action == "get"
        )
        deleting = (
            isinstance(request, _TaskMetadata) and request.action == "delete"
        )
        owned_sessions: frozenset[str] = frozenset()

        def authorize() -> None:
            nonlocal owned_sessions
            if deleting:
                check_effect()
                return
            owned_sessions = repository.authorize_task_metadata(
                lease,
                request.task_id,
                scopes=scopes,
                expires=expires,
                write=call.name != "read_todos",
            )
            if mutation:
                check_effect()

        def check_effect(*, claim: bool = False, receipt: dict | None = None):
            return repository.native_effect(
                lease,
                call,
                machine="control",
                scopes=scopes,
                expires=expires,
                registered=(),
                claim=claim,
                receipt=receipt,
            )

        def project(content: dict) -> dict:
            if deleting:
                body = content.get("result", content)
                if (
                    not isinstance(body, dict)
                    or body.get("deleted") is not True
                ):
                    raise NativeCreationUncertain(
                        "Native Task deletion is unconfirmed"
                    )
                deleted = TaskDeleteOutput.model_validate(body)
                if deleted.task_id != request.task_id:
                    raise NativeCreationUncertain(
                        "Native Task deletion receipt is mismatched"
                    )
                data = deleted.model_dump(mode="json")
            elif call.name in {"task", "task_plan"}:
                output = TaskOutput.model_validate(
                    content.get("result", content)
                    if call.name == "task"
                    else content
                )
                if output.task_id != request.task_id:
                    raise NativeCreationUncertain(
                        "Native Task receipt is mismatched"
                    )
                if isinstance(request, _TaskMetadata) and request.action in {
                    "finish",
                    "cancel",
                }:
                    expected = (
                        "completed"
                        if request.action == "finish"
                        else "cancelled"
                    )
                    if output.status != expected:
                        raise NativeCreationUncertain(
                            "Native Task terminal receipt is mismatched"
                        )
                output.session_ids = [
                    item
                    for item in output.session_ids
                    if item in owned_sessions
                ]
                data = output.model_dump(mode="json")
            else:
                data = ReadTodosOutput.model_validate(content).model_dump(
                    mode="json"
                )
            return CallToolResult(
                structured_content=data,
                content=[
                    TextContent(
                        type="text", text=json.dumps(data, ensure_ascii=False)
                    )
                ],
            ).model_dump(mode="json", by_alias=True, exclude_none=True)

        if mutation:
            fresh, receipt = check_effect(claim=True)
            if not fresh:
                if receipt is None:
                    raise NativeCreationUncertain(
                        "Native Task effect is unconfirmed; do not resend"
                    )
                authorize()
                return project(receipt["structuredContent"])
        authorize()
        result = await _await_authorized(
            self._mcp.call_tool(call.name, call.arguments), authorize
        )
        if (
            not isinstance(result, CallToolResult)
            or result.is_error
            or result.structured_content is None
        ):
            raise NativeCreationUncertain("Native Task result is unconfirmed")
        envelope = project(result.structured_content)
        if mutation:
            check_effect(receipt=envelope)
        return envelope
