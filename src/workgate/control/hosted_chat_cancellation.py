"""Private native cleanup for the final Hosted Chat durable cancellation ledger."""

import asyncio
import logging
from typing import Literal

from ..audit import audit
from ..errors import PublicToolValueError
from ..hosted_chat.durable_tasks import DurableTaskRepository
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
from ..schemas.result_models.jobs import ReservedJobAttemptReceipt
from ..schemas.result_models.shell import KillPersistentShellOutput
from .execution_context import control_execution_context
from .runtime import ControlRuntime
from .session_copy import SESSION_COPY_MANAGED_KIND

logger = logging.getLogger(__name__)


type CleanupOutcome = Literal["stopped", "absent", "pending"]


async def _stop_job(
    runtime: ControlRuntime, row: dict, key: str
) -> CleanupOutcome:
    session_id = row["native_session_id"]
    if not session_id:
        return "pending"
    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None or record.executor_id != row["machine"]:
        return "pending"
    if record.status == "ended":
        return "absent" if record.executor_absence_confirmed else "pending"
    if record.status != "active":
        return "pending"

    request = ReservedJobAttemptRequest(
        job_id=key,
        attempt=row["native_attempt"],
        retry_key=row["idempotency_key"] if row["kind"] == "retry" else None,
    )
    receipt = ReservedJobAttemptReceipt.model_validate(
        await runtime.session_coordinator.call_session_tool(
            JOB_CANCEL_ATTEMPT_OP,
            request.model_dump(mode="json") | {"session_id": session_id},
        )
    )
    if not receipt.matches(request, session_id):
        return "pending"
    if receipt.state == "superseded":
        return "absent"
    if receipt.state != "matching" or receipt.job is None:
        return "pending"
    return (
        "stopped"
        if receipt.job.status in CONFIRMED_TERMINAL_STATUSES
        else "pending"
    )


async def _stop_shell(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    row: dict,
    key: str,
) -> CleanupOutcome:
    session_id = row["native_session_id"]
    if not session_id:
        return "pending"
    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None or record.executor_id != row["machine"]:
        return "pending"
    if record.status == "ended":
        return "absent" if record.executor_absence_confirmed else "pending"
    if record.status != "active":
        return "pending"
    receipt = ReservedShellLookupResult.model_validate(
        await runtime.session_coordinator.call_session_tool(
            SHELL_LOOKUP_RESERVED_OP,
            {"session_id": session_id, "shell_id": key},
        )
    )
    if receipt.session_id != session_id or receipt.shell_id != key:
        return "pending"
    if not receipt.present:
        # A previously observed Shell is gone; an unobserved start might still
        # be in flight. Absence alone must not manufacture its receipt.
        return "absent" if row["native_id"] == key else "pending"
    if not repository.record_cleanup_receipt(row["id"], key):
        return "pending"
    row["native_id"] = key
    result = KillPersistentShellOutput.model_validate(
        await runtime.session_coordinator.call_session_tool(
            "kill_persistent_shell", {"session_id": session_id, "shell_id": key}
        )
    )
    return (
        "stopped"
        if result.shell_id == key and result.killed is True
        else "pending"
    )


async def _stop_copy(row: dict, key: str) -> CleanupOutcome:
    source = row["native_session_id"]
    destination = row["destination_session_id"]
    if not source or not destination:
        return "pending"
    # Managed work is owned by Control, even if either Executor is offline.
    request = ReservedJobAttemptRequest(
        job_id=key,
        attempt=row["native_attempt"],
        retry_key=row["idempotency_key"] if row["kind"] == "retry" else None,
    )
    binding = {"src_session_id": source, "dst_session_id": destination}
    await stop_managed_job_without_session_admission(
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
        return "absent"
    return (
        "stopped"
        if job is not None and job.status in CONFIRMED_TERMINAL_STATUSES
        else "pending"
    )


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
