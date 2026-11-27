"""Private native Job dispatch, never the unrestricted public Job service."""

import copy
from dataclasses import replace
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..protocol.ids import ReservedJobId, SessionId
from .hosted_chat_cancellation import cancel_native_jobs
from .hosted_chat_job_inspection import inspect_native_jobs
from .hosted_chat_native_creation import create_native
from .runtime import ControlRuntime


class NativeJobRequest(BaseModel):
    """Native Job surface with action-specific authorization and owned targets."""

    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: SessionId
    list_jobs: bool = False
    poll: list[ReservedJobId] | None = Field(
        default=None, min_length=1, max_length=64
    )
    cancel: list[ReservedJobId] | None = Field(
        default=None, min_length=1, max_length=64
    )
    retry: list[ReservedJobId] | None = Field(
        default=None, min_length=1, max_length=64
    )
    include_finished: bool = True
    lines: int = Field(default=200, ge=1, le=5000)
    limit: int = Field(default=25, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_operation(self) -> Self:
        selected = [
            targets
            for targets in (self.poll, self.cancel, self.retry)
            if targets is not None
        ]
        if len(selected) > 1 or (selected and self.list_jobs):
            raise ValueError("Job operations are mutually exclusive")
        if selected and len(set(selected[0])) != len(selected[0]):
            raise ValueError("Duplicate native Job target")
        return self


async def dispatch_native_job(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    call: ModelToolCall,
    *,
    machine: str,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    """Normalize public defaults and dispatch exactly one owned operation."""
    call = copy.deepcopy(call)
    complete_tool_batch([call])
    if call.name != "job":
        raise ValueError("Expected native Job operation")
    request = NativeJobRequest.model_validate(call.arguments)
    args: dict[str, Any] = {"session_id": request.session_id}
    if request.retry is not None:
        args["retry"] = request.retry
        execute = create_native
    elif request.cancel is not None:
        args["cancel"] = request.cancel
        execute = cancel_native_jobs
    else:
        if request.poll is not None:
            args.update(poll=request.poll, lines=request.lines)
        else:
            args.update(
                list_jobs=request.list_jobs,
                include_finished=request.include_finished,
                limit=request.limit,
                offset=request.offset,
            )
        execute = inspect_native_jobs
    call = replace(call, arguments=args)
    return await execute(
        runtime,
        repository,
        lease,
        call,
        machine=machine,
        scopes=scopes,
        expires=expires,
    )
