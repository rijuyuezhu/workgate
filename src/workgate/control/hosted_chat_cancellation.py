"""Private native cleanup for the final Hosted Chat durable cancellation ledger."""

import asyncio
import logging

from ..audit import audit
from ..errors import PublicToolValueError
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..oauth.core.context import bind_oauth_claims, reset_oauth_claims
from .execution_context import control_execution_context
from .runtime import ControlRuntime

logger = logging.getLogger(__name__)


async def _stop(runtime: ControlRuntime, row: dict) -> bool:
    # IDs were allocated by the private ledger before the one dispatch claim.
    # Never stop an arbitrary ID from a provider response or a listing.
    key = row["idempotency_key"]
    if not key or row["native_id"] not in (None, key):
        return False
    if row["kind"] == "session":
        return await runtime.session_coordinator.end_reserved_session(
            key, executor_id=row["machine"]
        )
    if row["kind"] != "task":
        return False
    # Bind only the persisted principal for the native Task owner check.
    # This is internal cleanup, not a new grant to model tools.
    token = bind_oauth_claims({"sub": row["owner"]})
    try:
        try:
            task = await runtime.task_service.read_task(key)
        except PublicToolValueError:
            return False
        if task.status in {"completed", "cancelled"}:
            return True
        result = await runtime.task_service.cancel_task(
            key, require_no_live_sessions=True
        )
        return result.status == "cancelled"
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
                    stopped = await asyncio.wait_for(
                        _stop(runtime, row), timeout=30
                    )
                except Exception as exc:
                    logger.debug(
                        "Native Chat cancellation pending (%s)",
                        type(exc).__name__,
                    )
                    stopped = False
                repository.finish_cancellation(row["id"], stopped=stopped)
                if stopped:
                    audit(
                        "agent_durable_cancel",
                        owner=row["owner"],
                        conversation_id=row["conversation_id"],
                        tool=row["tool"],
                        machine=row["machine"],
                        native_id=row["idempotency_key"],
                        ok=True,
                    )
                return int(stopped)

    return sum(await asyncio.gather(*(reconcile(row) for row in rows)))
