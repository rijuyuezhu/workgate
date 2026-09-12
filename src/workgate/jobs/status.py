"""Process-neutral durable job status observation and reconciliation helpers."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from ..config.settings import get_settings
from .reconciliation import (
    JobObservation,
    JobOperationKind,
    JobTransition,
    ManagedJobLiveness,
    ShellLiveness,
    reconcile_job,
)
from .state import (
    ACTIVE_STATUSES,
    JobStatusPayload,
    MutableJobRow,
)
from .state import clear_job_operation as _clear_job_operation
from .state import job_operation_is_active as _job_operation_is_active
from .state import job_shell_id as _job_shell_id
from .state import utc as _utc

type ManagedJobStateProbe = Callable[[Mapping[str, Any]], bool]
type ManagedJobLivenessProbe = Callable[[Mapping[str, Any]], str]
type JobStatusReader = Callable[[Mapping[str, Any]], JobStatusPayload | None]
type JobStatusPathReader = Callable[[Any], JobStatusPayload | None]


def _read_status_path(raw_path: Any) -> JobStatusPayload | None:
    if not raw_path:
        return None
    try:
        payload = json.loads(Path(str(raw_path)).read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    return (
        cast(JobStatusPayload, payload) if isinstance(payload, dict) else None
    )


def _read_status(job: Mapping[str, Any]) -> JobStatusPayload | None:
    return _read_status_path(job.get("status_path"))


def _clear_pending_retry(job: MutableJobRow) -> None:
    for key in (
        "pending_attempt",
        "pending_shell_id",
        "pending_command_path",
        "pending_log_path",
        "pending_status_path",
    ):
        job.pop(key, None)


def _adopt_pending_retry(job: MutableJobRow) -> None:
    attempt = job.get("pending_attempt")
    if attempt is not None:
        job["attempts"] = int(attempt)
    for pending_key, active_key in (
        ("pending_shell_id", "shell_id"),
        ("pending_command_path", "command_path"),
        ("pending_log_path", "log_path"),
        ("pending_status_path", "status_path"),
    ):
        value = job.get(pending_key)
        if value:
            job[active_key] = value


def _apply_job_transition(
    job: MutableJobRow, transition: JobTransition
) -> MutableJobRow:
    """Apply one pure reconciliation transition to a mutable durable row."""
    if transition.clear_operation:
        _clear_job_operation(job)
    for key in transition.remove_keys:
        job.pop(key, None)
    cast(dict[str, Any], job).update(transition.updates)
    return job


def _observed_shell_liveness(
    shell_id: str, active_shells: set[str] | None
) -> ShellLiveness:
    """Classify one shell id from an authoritative inventory observation."""
    if active_shells is None:
        return "unknown"
    return "live" if shell_id and shell_id in active_shells else "absent"


def _observed_active_operation(
    job: Mapping[str, Any],
) -> JobOperationKind | None:
    """Return the process-local operation token that still owns this row."""
    for operation in ("start", "stop", "retry"):
        if _job_operation_is_active(job, operation):
            return operation
    return None


def _refresh_job_status(
    job: MutableJobRow,
    active_shells: set[str] | None,
    now: float | None = None,
    *,
    managed_job_has_local_task: ManagedJobStateProbe,
    managed_job_liveness: ManagedJobLivenessProbe,
    read_status: JobStatusReader = _read_status,
    read_status_path: JobStatusPathReader = _read_status_path,
    observed_active_operation: Callable[
        [Mapping[str, Any]], JobOperationKind | None
    ] = _observed_active_operation,
) -> MutableJobRow:
    """Gather live observations, decide purely, then apply one transition."""
    status = str(job.get("status") or "unknown")
    kind = str(job.get("kind") or "shell")
    lost_shell = status == "lost" and kind == "shell"
    if not lost_shell and status not in ACTIVE_STATUSES:
        return job

    updated = now or _utc()
    active_operation = observed_active_operation(job)
    operation_blocks_observation = kind == "managed" or (
        (status == "starting" and active_operation == "start")
        or (status == "stopping" and active_operation == "stop")
        or (status == "retrying" and active_operation == "retry")
    )
    if active_operation is not None and operation_blocks_observation:
        return _apply_job_transition(
            job,
            reconcile_job(
                job,
                JobObservation(now=updated, active_operation=active_operation),
            ),
        )

    status_payload: JobStatusPayload | None = None
    pending_status_payload: JobStatusPayload | None = None
    shell_liveness: ShellLiveness = "unknown"
    managed_has_local_task = False
    managed_liveness: ManagedJobLiveness = "unknown"

    if kind == "managed":
        if active_operation is None:
            managed_has_local_task = managed_job_has_local_task(job)
            if not managed_has_local_task:
                managed_liveness = cast(
                    ManagedJobLiveness, managed_job_liveness(job)
                )
    elif status == "retrying":
        pending_status_payload = read_status_path(
            job.get("pending_status_path")
        )
        shell_liveness = _observed_shell_liveness(
            str(job.get("pending_shell_id") or ""), active_shells
        )
    else:
        status_payload = read_status(job)
        shell_liveness = _observed_shell_liveness(
            _job_shell_id(job), active_shells
        )

    transition = reconcile_job(
        job,
        JobObservation(
            now=updated,
            shell_liveness=shell_liveness,
            status_payload=status_payload,
            pending_status_payload=pending_status_payload,
            active_operation=active_operation,
            managed_has_local_task=managed_has_local_task,
            managed_liveness=managed_liveness,
        ),
    )
    return _apply_job_transition(job, transition)


def _read_log_tail(
    path: str | None,
    lines: int,
    *,
    max_bytes: int | None = None,
) -> str:
    """Read a bounded UTF-8 tail from a durable job log."""
    if not path:
        return ""
    target = Path(path)
    if not target.is_file():
        return ""
    byte_limit = max(
        1,
        int(
            get_settings().max_job_log_bytes if max_bytes is None else max_bytes
        ),
    )
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            if size > byte_limit:
                handle.seek(size - byte_limit)
            data = handle.read(byte_limit)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if lines > 0:
        selected = text.splitlines()[-max(1, lines) :]
        text = "\n".join(selected)
        if data.endswith((b"\n", b"\r")) and text:
            text += "\n"
    return text
