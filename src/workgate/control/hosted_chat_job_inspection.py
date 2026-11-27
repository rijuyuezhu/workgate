"""Read-only Hosted Chat projection of exact native Job attempts."""

import asyncio
import copy
import time
from collections import Counter
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
from ..jobs.managed import lookup_managed_job_attempt, managed_job_tail_execute
from ..jobs.persistence import TERMINAL_STATUSES
from ..protocol.executor import JOB_LIST_ATTEMPTS_OP, ReservedJobAttemptRequest
from ..protocol.ids import ReservedJobId, SessionId
from ..schemas.result_models.jobs import (
    JobInfo,
    JobOutput,
    JobTailOutput,
    ReservedJobAttemptBatchReceipt,
)
from .execution_context import control_execution_context
from .hosted_chat_native_creation import (
    NativeCreationUncertain,
    _grant,
    _job_attempt_receipt,
    _registered,
)
from .jobs import _list_page
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


class NativeJobListRequest(BaseModel):
    """List only owned, authorized current attempts."""

    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: SessionId
    list_jobs: bool = False
    include_finished: bool = True
    limit: int = Field(default=25, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


def _attempt_request(intent: Intent) -> ReservedJobAttemptRequest:
    if intent.native_id is None or intent.attempt is None:
        raise PermissionError("Missing native Job attempt binding")
    return ReservedJobAttemptRequest(
        job_id=intent.native_id,
        attempt=intent.attempt,
        retry_key=intent.key if intent.attempt > 1 else None,
    )


async def _list_all(
    runtime: ControlRuntime,
    request: NativeJobListRequest,
    intents: list[Intent],
    machine: str,
) -> list[JobInfo]:
    session_id = request.session_id
    coordinator = runtime.session_coordinator
    record = coordinator.require_session_status(session_id, {"active"})
    if record.executor_id != machine:
        raise PermissionError("Native Job Session binding mismatch")
    jobs = []
    shell = [
        _attempt_request(intent)
        for intent in intents
        if intent.destination_session_id is None
    ]
    for start in range(0, len(shell), 64):
        batch = shell[start : start + 64]
        expected = {item.job_id: item for item in batch}
        response = await coordinator.call_session_tool(
            JOB_LIST_ATTEMPTS_OP,
            {
                "session_id": session_id,
                "attempts": [item.model_dump(mode="json") for item in batch],
            },
        )
        for receipt in ReservedJobAttemptBatchReceipt.model_validate(
            response
        ).receipts:
            item = expected.pop(receipt.job_id, None)
            if (
                item is None
                or not receipt.matches(item, session_id)
                or receipt.state != "matching"
                or receipt.job is None
                or receipt.output is not None
            ):
                raise NativeCreationUncertain(
                    "Invalid native Job metadata receipt"
                )
            jobs.append(receipt.job)
    for intent in intents:
        destination = intent.destination_session_id
        if destination is None:
            continue
        expected_executors = {
            session_id: machine,
            destination: intent.destination_machine,
        }
        try:
            async with coordinator.session_admission(
                tuple(expected_executors), require_available=()
            ) as records:
                if expected_executors != {
                    str(record.session_id): str(record.executor_id)
                    for record in records
                }:
                    continue
                state, job = lookup_managed_job_attempt(
                    session_id,
                    _attempt_request(intent),
                    expected_kind=SESSION_COPY_MANAGED_KIND,
                    expected_payload={
                        "src_session_id": session_id,
                        "dst_session_id": destination,
                    },
                )
                if state == "matching" and job is not None:
                    jobs.append(job)
        except RuntimeError, ValueError, KeyError, PermissionError:
            continue
    return jobs


async def _poll_attempt(
    runtime: ControlRuntime, intent: Intent, lines: int
) -> JobTailOutput:
    session_id, job_id = intent.session_id, intent.native_id
    if session_id is None or job_id is None or intent.attempt is None:
        raise PermissionError("Missing native Job attempt binding")
    request = _attempt_request(intent)
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
    """Inspect only the conversation's exact authorized native attempts."""
    if repository.database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Native intent belongs to another Control runtime"
        )
    scopes, expires = _grant(runtime, lease.owner, scopes, expires)
    call = copy.deepcopy(call)
    complete_tool_batch([call])
    if call.name != "job":
        raise ValueError("Expected native Job inspection")
    request = (
        NativeJobPollRequest.model_validate(call.arguments)
        if "poll" in call.arguments
        else NativeJobListRequest.model_validate(call.arguments)
    )

    def authorize():
        if isinstance(request, NativeJobListRequest):
            return repository.list_attempts(
                lease,
                request.session_id,
                machine=machine,
                scopes=scopes,
                expires=expires,
                registered=_registered(runtime),
            )
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
                timeout = tool_timeout_s(
                    call.name, config=runtime.config, args=call.arguments
                )
                if isinstance(request, NativeJobListRequest):
                    jobs = await asyncio.wait_for(
                        _list_all(runtime, request, intents, machine),
                        timeout=timeout,
                    )
                    counts = dict(Counter(job.status for job in jobs))
                    if not request.include_finished:
                        jobs = [
                            job
                            for job in jobs
                            if job.status not in TERMINAL_STATUSES
                        ]
                    jobs.sort(
                        key=lambda job: (job.created_at, job.job_id),
                        reverse=True,
                    )
                    output = _list_page(
                        jobs,
                        counts,
                        offset=request.offset,
                        limit=request.limit,
                        message="Conversation-owned native Job snapshot.",
                    )
                else:
                    outputs = await asyncio.wait_for(
                        _poll_all(runtime, intents, request.lines),
                        timeout=timeout,
                    )
                    output = JobOutput(operation="poll", outputs=outputs)
            if authorize() != intents:
                raise NativeCreationUncertain(
                    "Native Job attempt changed during inspection"
                )
            result = output.model_dump(mode="json")
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
