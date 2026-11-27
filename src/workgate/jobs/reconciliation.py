"""Pure durable-job reconciliation decisions from pre-collected observations."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .state import ACTIVE_STATUSES, JobStatusPayload

type ShellLiveness = Literal["live", "absent", "unknown"]
type ManagedJobLiveness = Literal["live", "dead", "unknown"]
type JobOperationKind = Literal["start", "stop", "retry"]

_PENDING_RETRY_KEYS = (
    "pending_attempt",
    "pending_shell_id",
    "pending_command_path",
    "pending_log_path",
    "pending_status_path",
)


@dataclass(frozen=True)
class JobObservation:
    """External facts gathered before reconciling one durable job row."""

    now: float
    """Timestamp to use for recovery transitions decided by this observation."""
    shell_liveness: ShellLiveness = "unknown"
    """Authoritative shell presence, or unknown when inventory could not be read."""
    status_payload: JobStatusPayload | None = None
    """Durable completion payload for the active attempt, when available."""
    pending_status_payload: JobStatusPayload | None = None
    """Durable completion payload for a pending retry attempt, when available."""
    active_operation: JobOperationKind | None = None
    """Process-local start/stop/retry operation still owning the row, if any."""
    managed_has_local_task: bool = False
    """Whether this process still owns a running managed task for the row."""
    managed_liveness: ManagedJobLiveness = "unknown"
    """Cross-process managed-job lease liveness after local-task probing."""


@dataclass(frozen=True)
class JobTransition:
    """Pure mutation instructions produced by one reconciliation decision."""

    updates: Mapping[str, Any] = field(default_factory=dict)
    """Key/value pairs to write into the durable row."""
    remove_keys: tuple[str, ...] = ()
    """Durable/transient row keys to remove after applying updates."""
    clear_operation: bool = False
    """Whether the imperative shell must also release the operation token."""


def _completion_transition(
    payload: JobStatusPayload,
    now: float,
    *,
    updates: Mapping[str, Any] | None = None,
    remove_keys: tuple[str, ...] = (),
) -> JobTransition:
    """Return one terminal transition from a runner completion payload."""
    exit_code = payload.get("exit_code")
    completed_at = float(payload.get("completed_at") or now)
    merged = dict(updates or {})
    merged.update(
        {
            "status": "succeeded" if exit_code == 0 else "failed",
            "updated_at": completed_at,
            "completed_at": completed_at,
            "exit_code": exit_code,
            "error": payload.get("error"),
            "log_truncated": bool(payload.get("log_truncated", False)),
            "output_bytes": int(payload.get("output_bytes") or 0),
        }
    )
    return JobTransition(
        updates=merged,
        remove_keys=(*remove_keys, "shell_absence_confirmed"),
        clear_operation=True,
    )


def _pending_retry_updates(job: Mapping[str, Any]) -> dict[str, Any]:
    """Return active-attempt fields adopted from pending retry metadata."""
    updates: dict[str, Any] = {}
    attempt = job.get("pending_attempt")
    if attempt is not None:
        updates["attempts"] = int(attempt)
    for pending_key, active_key in (
        ("pending_shell_id", "shell_id"),
        ("pending_command_path", "command_path"),
        ("pending_log_path", "log_path"),
        ("pending_status_path", "status_path"),
    ):
        value = job.get(pending_key)
        if value:
            updates[active_key] = value
    return updates


def reconcile_job(
    job: Mapping[str, Any], observation: JobObservation
) -> JobTransition:
    """Decide one job-state transition without performing I/O or mutation."""
    status = str(job.get("status") or "unknown")
    kind = str(job.get("kind") or "shell")

    if status == "lost" and kind == "shell":
        if observation.status_payload is not None:
            return _completion_transition(
                observation.status_payload, observation.now
            )
        if observation.shell_liveness == "unknown":
            return JobTransition()
        if observation.shell_liveness == "live":
            return JobTransition(
                updates={
                    "status": "running",
                    "updated_at": observation.now,
                    "completed_at": None,
                    "exit_code": None,
                    "error": "recovered a lost job whose shell is still active",
                },
                remove_keys=("shell_absence_confirmed",),
            )
        return JobTransition(updates={"shell_absence_confirmed": True})

    if status not in ACTIVE_STATUSES:
        return JobTransition()

    if kind == "managed":
        if observation.active_operation is not None:
            return JobTransition()
        if observation.managed_has_local_task:
            return JobTransition()
        if observation.managed_liveness != "dead":
            return JobTransition()
        stopped = status == "stopping"
        retrying = status == "retrying"
        return JobTransition(
            updates={
                **(_pending_retry_updates(job) if retrying else {}),
                "status": "stopped" if stopped else "lost",
                "updated_at": observation.now,
                "completed_at": observation.now,
                "exit_code": None,
                "error": (
                    None
                    if stopped
                    else "managed job is no longer running; retry it to resume the operation"
                ),
            },
            remove_keys=_PENDING_RETRY_KEYS if retrying else (),
            clear_operation=True,
        )

    expected_operation: JobOperationKind | None = None
    if status == "starting":
        expected_operation = "start"
    elif status == "stopping":
        expected_operation = "stop"
    elif status == "retrying":
        expected_operation = "retry"
    if (
        expected_operation is not None
        and observation.active_operation == expected_operation
    ):
        return JobTransition()

    if status == "starting":
        if observation.status_payload is not None:
            return _completion_transition(
                observation.status_payload, observation.now
            )
        if observation.shell_liveness == "unknown":
            return JobTransition()
        if observation.shell_liveness == "live":
            return JobTransition(
                updates={
                    "status": "running",
                    "updated_at": observation.now,
                    "last_started_at": job.get("last_started_at")
                    or observation.now,
                    "completed_at": None,
                    "exit_code": None,
                    "error": "recovered job start after an interrupted state commit",
                },
                clear_operation=True,
            )
        return JobTransition(
            updates={
                "status": "failed",
                "updated_at": observation.now,
                "completed_at": observation.now,
                "exit_code": None,
                "error": "job start was interrupted before a recoverable shell was created",
            },
            clear_operation=True,
        )

    if status == "retrying":
        pending_updates = _pending_retry_updates(job)
        if observation.pending_status_payload is not None:
            return _completion_transition(
                observation.pending_status_payload,
                observation.now,
                updates=pending_updates,
                remove_keys=_PENDING_RETRY_KEYS,
            )
        if observation.shell_liveness == "unknown":
            return JobTransition()
        if observation.shell_liveness == "live":
            return JobTransition(
                updates={
                    **pending_updates,
                    "status": "running",
                    "updated_at": observation.now,
                    "last_started_at": observation.now,
                    "completed_at": None,
                    "exit_code": None,
                    "error": "recovered retry after an interrupted state commit",
                },
                remove_keys=_PENDING_RETRY_KEYS,
                clear_operation=True,
            )
        return JobTransition(
            updates={
                **pending_updates,
                "status": "failed",
                "updated_at": observation.now,
                "completed_at": observation.now,
                "exit_code": None,
                "error": "retry was interrupted before a recoverable shell was committed",
            },
            remove_keys=_PENDING_RETRY_KEYS,
            clear_operation=True,
        )

    if observation.status_payload is not None:
        return _completion_transition(
            observation.status_payload, observation.now
        )
    if observation.shell_liveness == "unknown":
        return JobTransition()

    if status == "stopping":
        if observation.shell_liveness == "live":
            return JobTransition(
                updates={
                    "status": "running",
                    "updated_at": observation.now,
                    "error": "recovered an interrupted stop request; shell is still active",
                },
                clear_operation=True,
            )
        return JobTransition(
            updates={
                "status": "stopped",
                "updated_at": observation.now,
                "completed_at": observation.now,
                "exit_code": None,
                "error": None,
            },
            clear_operation=True,
        )

    if observation.shell_liveness == "live":
        return JobTransition()
    return JobTransition(
        updates={
            "status": "lost",
            "updated_at": observation.now,
            "completed_at": observation.now,
            "exit_code": None,
            "error": "job shell exited without a durable completion record",
            "shell_absence_confirmed": True,
        }
    )
