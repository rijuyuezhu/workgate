"""Private native cleanup for the final Hosted Chat durable cancellation ledger."""

import asyncio
import copy
import logging
import time
from typing import Any, Literal

from ..audit import (
    audit,
    audit_call_context,
    audit_tool_call_end,
    audit_tool_call_start,
    new_audit_call_id,
)
from ..errors import PublicToolValueError
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall
from ..jobs.managed import (
    lookup_managed_job_attempt,
    stop_managed_job_without_session_admission,
)
from ..jobs.state import CONFIRMED_TERMINAL_STATUSES
from ..oauth.core.context import bind_oauth_claims, reset_oauth_claims
from ..protocol.executor import (
    JOB_CANCEL_ATTEMPT_OP,
    SHELL_LOOKUP_RESERVED_OP,
    ReservedJobAttemptRequest,
    ReservedShellLookupResult,
)
from ..schemas.result_models.jobs import (
    JobOutput,
    JobStopOutput,
    ReservedJobCancelReceipt,
)
from ..schemas.result_models.session import SessionEndOutput
from ..schemas.result_models.shell import KillPersistentShellOutput
from .execution_context import control_execution_context
from .hosted_chat_native_creation import (
    NativeCreationUncertain,
    _grant,
    _registered,
)
from .runtime import ControlRuntime
from .session_copy import SESSION_COPY_MANAGED_KIND
from .tool_timeouts import tool_timeout_s

logger = logging.getLogger(__name__)


type CleanupOutcome = Literal["stopped", "absent", "pending"]


async def _stop_job_result(
    runtime: ControlRuntime, row: dict, key: str
) -> tuple[CleanupOutcome, JobStopOutput | None]:
    session_id = row["native_session_id"]
    if not session_id:
        return "pending", None
    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None or record.executor_id != row["machine"]:
        return "pending", None
    if record.status == "ended":
        return (
            "absent" if record.executor_absence_confirmed else "pending"
        ), None
    if record.status != "active":
        return "pending", None

    request = ReservedJobAttemptRequest(
        job_id=key,
        attempt=row["native_attempt"],
        retry_key=row["idempotency_key"] if row["kind"] == "retry" else None,
    )
    receipt = ReservedJobCancelReceipt.model_validate(
        await runtime.session_coordinator.call_session_tool(
            JOB_CANCEL_ATTEMPT_OP,
            request.model_dump(mode="json") | {"session_id": session_id},
        )
    )
    if not receipt.matches(request, session_id):
        return "pending", None
    if receipt.state == "superseded":
        return "absent", None
    if receipt.state != "matching" or receipt.job is None:
        return "pending", None
    return (
        "stopped"
        if receipt.job.status in CONFIRMED_TERMINAL_STATUSES
        else "pending"
    ), receipt.stop


async def _stop_job(
    runtime: ControlRuntime, row: dict, key: str
) -> CleanupOutcome:
    outcome, _ = await _stop_job_result(runtime, row, key)
    return outcome


async def _stop_shell_result(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    row: dict,
    key: str,
) -> tuple[CleanupOutcome, KillPersistentShellOutput | None]:
    session_id = row["native_session_id"]
    if not session_id:
        return "pending", None
    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None or record.executor_id != row["machine"]:
        return "pending", None
    if record.status == "ended":
        return (
            "absent" if record.executor_absence_confirmed else "pending"
        ), None
    if record.status != "active":
        return "pending", None
    receipt = ReservedShellLookupResult.model_validate(
        await runtime.session_coordinator.call_session_tool(
            SHELL_LOOKUP_RESERVED_OP,
            {"session_id": session_id, "shell_id": key},
        )
    )
    if receipt.session_id != session_id or receipt.shell_id != key:
        return "pending", None
    if not receipt.present:
        # A previously observed Shell is gone; an unobserved start might still
        # be in flight. Absence alone must not manufacture its receipt.
        return ("absent" if row["native_id"] == key else "pending"), None
    if not repository.record_cleanup_receipt(row["id"], key):
        return "pending", None
    row["native_id"] = key
    result = KillPersistentShellOutput.model_validate(
        await runtime.session_coordinator.call_session_tool(
            "kill_persistent_shell", {"session_id": session_id, "shell_id": key}
        )
    )
    if result.shell_id == key and result.killed is True:
        return "stopped", result
    return "pending", None


async def _stop_shell(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    row: dict,
    key: str,
) -> CleanupOutcome:
    outcome, _ = await _stop_shell_result(runtime, repository, row, key)
    return outcome


async def _stop_copy_result(
    row: dict, key: str
) -> tuple[CleanupOutcome, JobStopOutput | None]:
    source = row["native_session_id"]
    destination = row["destination_session_id"]
    if not source or not destination:
        return "pending", None
    # Managed work is owned by Control, even if either Executor is offline.
    request = ReservedJobAttemptRequest(
        job_id=key,
        attempt=row["native_attempt"],
        retry_key=row["idempotency_key"] if row["kind"] == "retry" else None,
    )
    binding = {"src_session_id": source, "dst_session_id": destination}
    result = await stop_managed_job_without_session_admission(
        source,
        key,
        expected_attempt=request.attempt,
        expected_retry_key=request.retry_key,
        expected_kind=SESSION_COPY_MANAGED_KIND,
        expected_payload=binding,
    )
    state, job = lookup_managed_job_attempt(
        source,
        request,
        expected_kind=SESSION_COPY_MANAGED_KIND,
        expected_payload=binding,
    )
    if state == "superseded":
        return "absent", None
    return (
        (
            "stopped"
            if job is not None and job.status in CONFIRMED_TERMINAL_STATUSES
            else "pending"
        ),
        result.model_copy(update={"job": job})
        if job is not None and job.attempts == result.job.attempts
        else None,
    )


async def _stop_copy(row: dict, key: str) -> CleanupOutcome:
    outcome, _ = await _stop_copy_result(row, key)
    return outcome


async def _stop(
    runtime: ControlRuntime, repository: DurableTaskRepository, row: dict
) -> CleanupOutcome:
    # IDs were allocated by the private ledger before the one dispatch claim.
    # Never stop an arbitrary ID from a provider response or a listing.
    key = row["target_job_id"] or row["idempotency_key"]
    if not key or row["native_id"] not in (None, key):
        return "pending"
    if row["kind"] == "copy" or (
        row["kind"] == "retry" and row["destination_session_id"] is not None
    ):
        return await _stop_copy(row, key)
    if row["kind"] in {"job", "retry"}:
        return await _stop_job(runtime, row, key)
    if row["kind"] == "shell":
        return await _stop_shell(runtime, repository, row, key)
    if row["kind"] == "session":
        stopped = await runtime.session_coordinator.end_reserved_session(
            key, executor_id=row["machine"]
        )
        if stopped:
            runtime.agent_bridge.invalidate_discovery(key)
        return "stopped" if stopped else "pending"
    if row["kind"] != "task":
        return "pending"
    # Bind only the persisted principal for the native Task owner check.
    # This is internal cleanup, not a new grant to model tools.
    token = bind_oauth_claims({"sub": row["owner"]})
    try:
        try:
            task = await runtime.task_service.read_task(key)
        except PublicToolValueError:
            return "pending"
        if task.status in {"completed", "cancelled"}:
            return "stopped"
        result = await runtime.task_service.cancel_task(
            key, require_no_live_sessions=True
        )
        return "stopped" if result.status == "cancelled" else "pending"
    finally:
        reset_oauth_claims(token)


async def sweep_native_cancellations(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
) -> int:
    """One bounded sweep by the Control owner; no worker is started here."""
    if repository.database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Native intent belongs to another Control runtime"
        )
    registered = tuple(
        key
        for key, record in runtime.control_state.snapshot_executors().items()
        if record.revoked_at is None
    )
    rows = repository.cancellation_batch(registered, limit=8)
    semaphore = asyncio.Semaphore(4)

    async def reconcile(row: dict) -> int:
        async with semaphore:
            with control_execution_context(
                config=runtime.config,
                state_store=runtime.state_store,
                oauth_state=runtime.oauth_state,
                managed_jobs_runtime=runtime.managed_jobs_runtime,
            ):
                try:
                    outcome = await asyncio.wait_for(
                        _stop(runtime, repository, row), timeout=30
                    )
                except Exception as exc:
                    logger.debug(
                        "Native Chat cancellation pending (%s)",
                        type(exc).__name__,
                    )
                    outcome = "pending"
                stopped = outcome != "pending"
                repository.finish_cancellation(
                    row["id"],
                    stopped=stopped,
                    native_confirmed=outcome == "stopped",
                )
                if stopped:
                    audit(
                        "agent_durable_cancel",
                        owner=row["owner"],
                        conversation_id=row["conversation_id"],
                        tool=row["tool"],
                        machine=row["machine"],
                        native_id=(
                            row["target_job_id"] or row["idempotency_key"]
                        )
                        if outcome == "stopped"
                        else row["native_id"],
                        reserved_id=row["idempotency_key"],
                        ok=True,
                    )
                return int(stopped)

    return sum(await asyncio.gather(*(reconcile(row) for row in rows)))


async def _cancel_native(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    call: ModelToolCall,
    *,
    machine: str,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    """Persist explicit exact-attempt cancellation before native dispatch."""
    if repository.database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Native intent belongs to another Control runtime"
        )
    scopes, expires = _grant(runtime, lease.owner, scopes, expires)
    call = copy.deepcopy(call)

    def authorize():
        return repository.cancel_attempts(
            lease,
            call,
            machine=machine,
            scopes=scopes,
            expires=expires,
            registered=_registered(runtime),
        )

    rows = authorize()

    async def stop_all():
        if call.name == "session_end":
            row = rows[0]
            if row["state"] == "stopped":
                outcome = "stopped"
            else:
                outcome = await _stop(runtime, repository, row)
                repository.finish_cancellation(
                    row["id"],
                    stopped=outcome == "stopped",
                )
            if outcome != "stopped":
                raise NativeCreationUncertain(
                    "Native Session close is unconfirmed"
                )
            # A positive absence receipt confirms the Session ended, not
            # which individual children were killed by that invocation.
            return SessionEndOutput(
                session_id=row["native_id"],
                executor_id=machine,
                ended=True,
                force_released=False,
            ).model_dump(mode="json", exclude_unset=True)
        if call.name == "kill_persistent_shell":
            row = rows[0]
            key = row["native_id"]
            outcome, result = await _stop_shell_result(
                runtime, repository, row, key
            )
            repository.finish_cancellation(
                row["id"],
                stopped=outcome != "pending",
                native_confirmed=outcome == "stopped",
            )
            if outcome == "absent":
                return KillPersistentShellOutput(
                    shell_id=key,
                    killed=False,
                    stderr="Native Shell is confirmed absent; no process was killed.",
                ).model_dump(mode="json")
            if outcome != "stopped" or result is None:
                raise NativeCreationUncertain(
                    "Native Shell cancellation is unconfirmed"
                )
            return result.model_dump(mode="json")
        results = []
        uncertain = False
        for row in rows:
            # All targets have durable cancellation intent already. Do not
            # abandon later siblings merely because a response was lost.
            key = row["native_id"]
            try:
                if row["destination_session_id"] is not None:
                    outcome, result = await _stop_copy_result(row, key)
                else:
                    outcome, result = await _stop_job_result(runtime, row, key)
            except (
                RuntimeError,
                ValueError,
                KeyError,
                PermissionError,
                TimeoutError,
            ):
                outcome, result = "pending", None
            repository.finish_cancellation(
                row["id"],
                stopped=outcome != "pending",
                native_confirmed=outcome == "stopped",
            )
            if outcome != "stopped" or result is None:
                uncertain = True
            else:
                results.append(result)
        if uncertain:
            raise NativeCreationUncertain(
                "Native Job cancellation is unconfirmed or superseded"
            )
        return JobOutput(operation="cancel", cancelled=results).model_dump(
            mode="json"
        )

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
                result = await asyncio.wait_for(
                    stop_all(),
                    timeout=tool_timeout_s(
                        call.name, config=runtime.config, args=call.arguments
                    ),
                )
            authorize()
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


async def cancel_native_jobs(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    call: ModelToolCall,
    *,
    machine: str,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    if call.name != "job":
        raise ValueError("Expected native Job cancellation")
    return await _cancel_native(
        runtime,
        repository,
        lease,
        call,
        machine=machine,
        scopes=scopes,
        expires=expires,
    )


async def cancel_native_shell(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    call: ModelToolCall,
    *,
    machine: str,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    if call.name != "kill_persistent_shell":
        raise ValueError("Expected native Shell cancellation")
    return await _cancel_native(
        runtime,
        repository,
        lease,
        call,
        machine=machine,
        scopes=scopes,
        expires=expires,
    )


async def close_native_session(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    call: ModelToolCall,
    *,
    machine: str,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    if call.name != "session_end":
        raise ValueError("Expected native Session close")
    return await _cancel_native(
        runtime,
        repository,
        lease,
        call,
        machine=machine,
        scopes=scopes,
        expires=expires,
    )
