"""Private native cleanup for the final Hosted Chat durable cancellation ledger."""

import asyncio
import logging
from typing import Literal

from ..audit import audit
from ..errors import PublicToolValueError
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..jobs.state import CONFIRMED_TERMINAL_STATUSES
from ..oauth.core.context import bind_oauth_claims, reset_oauth_claims
from ..protocol.executor import (
    SHELL_LOOKUP_RESERVED_OP,
    ReservedShellLookupResult,
)
from ..schemas.result_models.jobs import JobOutput
from ..schemas.result_models.shell import KillPersistentShellOutput
from .execution_context import control_execution_context
from .runtime import ControlRuntime

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

    # A claimed but unreturned creation must be observed by exact native poll
    # before we attempt cancellation. Never replay the native creation.
    if row["native_id"] is None:
        receipt = JobOutput.model_validate(
            await runtime.session_coordinator.call_session_tool(
                "job", {"session_id": session_id, "poll": [key], "lines": 1}
            )
        )
        if receipt.operation != "poll" or len(receipt.outputs) != 1:
            return "pending"
        job = receipt.outputs[0].job
        if (
            job.job_id != key
            or job.session_id != session_id
            or job.kind != "shell"
        ):
            return "pending"
        if job.status in CONFIRMED_TERMINAL_STATUSES:
            return "stopped"

    result = JobOutput.model_validate(
        await runtime.session_coordinator.call_session_tool(
            "job", {"session_id": session_id, "cancel": [key], "lines": 1}
        )
    )
    if result.operation != "cancel" or len(result.cancelled) != 1:
        return "pending"
    job = result.cancelled[0].job
    if job.job_id != key or job.session_id != session_id or job.kind != "shell":
        return "pending"
    return "stopped" if job.status in CONFIRMED_TERMINAL_STATUSES else "pending"


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


async def _stop(
    runtime: ControlRuntime, repository: DurableTaskRepository, row: dict
) -> CleanupOutcome:
    # IDs were allocated by the private ledger before the one dispatch claim.
    # Never stop an arbitrary ID from a provider response or a listing.
    key = row["idempotency_key"]
    if not key or row["native_id"] not in (None, key):
        return "pending"
    if row["kind"] == "job":
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
                        native_id=row["idempotency_key"]
                        if outcome == "stopped"
                        else row["native_id"],
                        reserved_id=row["idempotency_key"],
                        ok=True,
                    )
                return int(stopped)

    return sum(await asyncio.gather(*(reconcile(row) for row in rows)))
