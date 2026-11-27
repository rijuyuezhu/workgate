"""Read-only Hosted Chat projection of exact native Job attempts."""

import asyncio
import copy
import time
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..audit import (
    audit_call_context,
    audit_tool_call_end,
    audit_tool_call_start,
    new_audit_call_id,
)
from ..hosted_chat.durable_tasks import DurableTaskRepository, Intent
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..jobs.managed import managed_job_tail_execute
from ..protocol.executor import ReservedJobAttemptRequest
from ..protocol.ids import ReservedJobId, SessionId
from ..schemas.result_models.jobs import JobOutput, JobTailOutput
from .execution_context import control_execution_context
from .hosted_chat_native_creation import (
    NativeCreationUncertain,
    _grant,
    _job_attempt_receipt,
    _registered,
)
from .runtime import ControlRuntime
from .session_copy import SESSION_COPY_MANAGED_KIND
from .tool_timeouts import tool_timeout_s


class NativeJobPollRequest(BaseModel):
    """Model-facing subset of the existing Job companion read operation."""

    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: SessionId
    poll: list[ReservedJobId] = Field(min_length=1, max_length=64)
    lines: int = Field(default=200, ge=1, le=5000)

    @model_validator(mode="after")
    def distinct_targets(self) -> Self:
        if len(set(self.poll)) != len(self.poll):
            raise ValueError("Duplicate native Job poll target")
        return self


async def _poll_attempt(
    runtime: ControlRuntime, intent: Intent, lines: int
) -> JobTailOutput:
    session_id, job_id = intent.session_id, intent.native_id
    if session_id is None or job_id is None or intent.attempt is None:
        raise PermissionError("Missing native Job attempt binding")
    request = ReservedJobAttemptRequest(
        job_id=job_id,
        attempt=intent.attempt,
        retry_key=intent.key if intent.attempt > 1 else None,
    )
    if intent.destination_session_id is None:
        receipt = await _job_attempt_receipt(
            runtime, intent, request, dispatch=False, lines=lines
        )
        assert receipt.job is not None
        return JobTailOutput(job=receipt.job, output=receipt.output or "")
    destination = intent.destination_session_id
    expected_executors = {
        session_id: intent.machine,
        destination: intent.destination_machine,
    }
    async with runtime.session_coordinator.session_admission(
        tuple(expected_executors),
        require_available=(),
    ) as records:
        if expected_executors != {
            str(record.session_id): str(record.executor_id)
            for record in records
        }:
            raise PermissionError("Managed copy Session bindings changed")
        try:
            return await managed_job_tail_execute(
                session_id,
                job_id,
                lines,
                reservation=request,
                expected_kind=SESSION_COPY_MANAGED_KIND,
                expected_payload={
                    "src_session_id": session_id,
                    "dst_session_id": destination,
                },
            )
        except RuntimeError, ValueError, KeyError:
            raise NativeCreationUncertain(
                "Native managed Job attempt is unconfirmed or superseded"
            ) from None


async def inspect_native_jobs(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    call: ModelToolCall,
    *,
    machine: str,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    """Poll only the conversation's exact authorized native attempts."""
    if repository.database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Native intent belongs to another Control runtime"
        )
    scopes, expires = _grant(runtime, lease.owner, scopes, expires)
    call = copy.deepcopy(call)
    complete_tool_batch([call])
    if call.name != "job":
        raise ValueError("Expected native Job poll")
    request = NativeJobPollRequest.model_validate(call.arguments)

    def authorize():
        return repository.poll_attempts(
            lease,
            request.session_id,
            request.poll,
            machine=machine,
            scopes=scopes,
            expires=expires,
            registered=_registered(runtime),
        )

    intents = authorize()
    with control_execution_context(
        config=runtime.config,
        state_store=runtime.state_store,
        oauth_state=runtime.oauth_state,
        managed_jobs_runtime=runtime.managed_jobs_runtime,
    ):
        audit_id = new_audit_call_id()
        start = time.monotonic()
        sessions, tasks = audit_tool_call_start(
            call_id=audit_id,
            transport="hosted-chat",
            tool=call.name,
            input=call.arguments,
        )
        try:
            with audit_call_context(audit_id, sessions, tasks):
                outputs = await asyncio.wait_for(
                    _poll_all(runtime, intents, request.lines),
                    timeout=tool_timeout_s(
                        call.name, config=runtime.config, args=call.arguments
                    ),
                )
            if [intent.id for intent in authorize()] != [
                intent.id for intent in intents
            ]:
                raise NativeCreationUncertain(
                    "Native Job attempt changed during poll"
                )
            result = JobOutput(operation="poll", outputs=outputs).model_dump(
                mode="json"
            )
        except BaseException as exc:
            audit_tool_call_end(
                call_id=audit_id,
                transport="hosted-chat",
                tool=call.name,
                ok=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                error={"type": type(exc).__name__},
                session_ids=sessions,
                task_ids=tasks,
            )
            raise
        audit_tool_call_end(
            call_id=audit_id,
            transport="hosted-chat",
            tool=call.name,
            ok=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            output=result,
            session_ids=sessions,
            task_ids=tasks,
        )
        return result


async def _poll_all(
    runtime: ControlRuntime, intents: list[Intent], lines: int
) -> list[JobTailOutput]:
    return [await _poll_attempt(runtime, intent, lines) for intent in intents]
