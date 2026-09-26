"""Shared tracked-job row identity, operation, and projection helpers."""

import json
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict, cast

from ..schemas.result_models.jobs import JobInfo
from ..utils.serialization import to_jsonable

ACTIVE_STATUSES = {"starting", "running", "stopping", "retrying"}
CONFIRMED_TERMINAL_STATUSES = {"succeeded", "failed", "exited", "stopped"}
MANAGED_STATE_MAX_BYTES = 262_144
_ACTIVE_JOB_OPERATIONS: set[str] = set()


class JobRow(TypedDict, total=False):
    """Known durable tracked-job fields, including tolerated legacy partial rows."""

    job_id: str
    """Opaque durable job identifier."""
    kind: str
    """Execution kind such as shell or managed."""
    name: str
    """Human-readable job name."""
    status: str
    """Current durable lifecycle status."""
    command: str
    """Command or managed-operation label shown to callers."""
    cwd: str
    """Workspace directory associated with the job."""
    session_id: str
    """Owning public agent-session identifier."""
    shell_id: str | None
    """Persistent-shell identifier for shell-backed jobs."""
    backend: str | None
    """Execution backend label persisted with the job."""
    command_path: str | None
    """Private command artifact path for the active attempt."""
    log_path: str | None
    """Private bounded log path for the active attempt."""
    status_path: str | None
    """Private runner status path for the active attempt."""
    created_at: float
    """Unix timestamp when the job was created."""
    updated_at: float
    """Unix timestamp of the latest durable update."""
    last_started_at: float | None
    """Unix timestamp of the latest attempt start."""
    completed_at: float | None
    """Unix timestamp when the job reached a terminal state."""
    exit_code: int | None
    """Terminal process-style exit code when one exists."""
    error: str | None
    """Bounded public-safe terminal error text."""
    log_truncated: bool
    """Whether retained log output was truncated by the size bound."""
    output_bytes: int
    """Total output bytes observed for the current attempt."""
    attempts: int
    """Number of attempts started for this durable job."""
    managed_kind: str
    """Registered managed-job handler kind."""
    managed_payload: dict[str, Any]
    """Bounded extensible JSON payload supplied to a managed handler."""
    runtime_instance_id: str
    """Control runtime instance that most recently owned the managed task."""
    managed_lease_version: int
    """Managed-job lease protocol version persisted with liveness metadata."""
    progress: dict[str, Any] | None
    """Latest bounded extensible managed-job progress object."""
    result: dict[str, Any] | None
    """Terminal bounded extensible managed-job result object."""
    shell_absence_confirmed: bool
    """Whether authoritative shell inventory confirmed a lost shell is absent."""
    operation_id: str
    """Process-local operation token temporarily published into the row."""
    operation_kind: str
    """Lifecycle operation associated with the current operation token."""
    operation_started_at: float
    """Unix timestamp when the current lifecycle operation began."""
    pending_attempt: int
    """Retry attempt number staged before an atomic shell transition."""
    pending_shell_id: str
    """Retry shell identifier staged before adoption."""
    pending_command_path: str
    """Retry command artifact path staged before adoption."""
    pending_log_path: str
    """Retry log artifact path staged before adoption."""
    pending_status_path: str
    """Retry status artifact path staged before adoption."""
    managed_deferred_update_ids: list[str]
    """Applied managed journal update ids retained for idempotent replay."""


class JobStore(TypedDict):
    """Current durable jobs.json envelope."""

    version: int
    """Durable job-store schema version."""
    jobs: list[JobRow]
    """Tracked job rows retained by current policy."""


class JobAttemptPaths(TypedDict):
    """Private filesystem paths for one shell or managed attempt."""

    command: Path
    """Command payload file for the attempt."""
    log: Path
    """Bounded output log file for the attempt."""
    status: Path
    """Terminal runner status JSON file for the attempt."""


class JobStatusPayload(TypedDict, total=False):
    """Bounded terminal metadata written by the standalone shell runner."""

    exit_code: int | None
    """Process exit code, or None when execution failed before one existed."""
    completed_at: float
    """Unix timestamp when the runner stopped."""
    error: str | None
    """Public-safe runner failure text, when present."""
    log_truncated: bool
    """Whether the runner truncated retained log bytes."""
    output_bytes: int
    """Total output bytes observed by the runner."""


type MutableJobRow = JobRow | dict[str, Any]
type MutableJobStore = JobStore | dict[str, Any]


def utc() -> float:
    """Return the current Unix timestamp."""
    return time.time()


def new_job_id() -> str:
    """Return one opaque durable job identifier."""
    return "job_" + uuid.uuid4().hex[:12]


def job_shell_id(job: Mapping[str, Any]) -> str:
    """Return one job's internal persistent-shell identifier."""
    value = job.get("shell_id")
    return str(value) if value else ""


def job_agent_session_id(job: Mapping[str, Any]) -> str | None:
    """Return the owning public agent-session id when present."""
    value = job.get("session_id")
    return value if isinstance(value, str) else None


def begin_job_operation(job: MutableJobRow, kind: str) -> str:
    """Publish one process-local operation token into a mutable durable row."""
    operation_id = f"{kind}_{uuid.uuid4().hex}"
    _ACTIVE_JOB_OPERATIONS.add(operation_id)
    job["operation_id"] = operation_id
    job["operation_kind"] = kind
    job["operation_started_at"] = utc()
    return operation_id


def job_operation_matches(job: Mapping[str, Any], operation_id: str) -> bool:
    """Return whether the row still contains one expected operation token."""
    return str(job.get("operation_id") or "") == operation_id


def job_operation_is_active(job: Mapping[str, Any], kind: str) -> bool:
    """Return whether a row operation is still owned by this process."""
    operation_id = str(job.get("operation_id") or "")
    return (
        bool(operation_id)
        and str(job.get("operation_kind") or "") == kind
        and operation_id in _ACTIVE_JOB_OPERATIONS
    )


def discard_job_operation(operation_id: str) -> None:
    """Forget one process-local operation token after its lifecycle ends."""
    _ACTIVE_JOB_OPERATIONS.discard(operation_id)


def clear_job_operation(job: MutableJobRow) -> None:
    """Remove transient operation metadata from one durable row."""
    operation_id = str(job.get("operation_id") or "")
    if operation_id:
        discard_job_operation(operation_id)
    for key in ("operation_id", "operation_kind", "operation_started_at"):
        job.pop(key, None)


def public_job(job: Mapping[str, Any]) -> JobInfo:
    """Return typed public metadata without exposing shell or runtime paths."""
    return JobInfo.model_validate(
        {
            "job_id": str(job.get("job_id") or ""),
            "kind": str(job.get("kind") or "shell"),
            "name": str(job.get("name") or job.get("job_id") or ""),
            "status": str(job.get("status") or "unknown"),
            "command": str(job.get("command") or ""),
            "cwd": str(job.get("cwd") or "."),
            "session_id": str(job.get("session_id") or ""),
            "progress": job.get("progress"),
            "result": job.get("result"),
            "created_at": float(job.get("created_at") or 0),
            "updated_at": float(job.get("updated_at") or 0),
            "last_started_at": (
                float(job.get("last_started_at") or 0)
                if job.get("last_started_at") is not None
                else None
            ),
            "completed_at": (
                float(job.get("completed_at") or 0)
                if job.get("completed_at") is not None
                else None
            ),
            "exit_code": (
                int(job.get("exit_code") or 0)
                if job.get("exit_code") is not None
                else None
            ),
            "error": str(job.get("error"))
            if job.get("error") is not None
            else None,
            "log_truncated": bool(job.get("log_truncated", False)),
            "output_bytes": int(job.get("output_bytes") or 0),
            "attempts": int(job.get("attempts") or 1),
        }
    )


def session_jobs(jobs: list[JobRow], session_id: str) -> list[JobRow]:
    """Return rows owned by one public agent session."""
    return [row for row in jobs if job_agent_session_id(row) == session_id]


def find_session_job(
    store: Mapping[str, Any], session_id: str, job_id: str
) -> JobRow:
    """Return one durable row owned by the requested public session."""
    for row in store.get("jobs", []):
        if (
            row.get("job_id") == job_id
            and job_agent_session_id(row) == session_id
        ):
            return cast(JobRow, row)
    raise KeyError(f"job not found in session: {job_id}")


def managed_json_dict(value: Any, *, label: str) -> dict[str, Any]:
    """Normalize and bound one managed-job payload, progress, or result object."""
    normalized = to_jsonable(value)
    if not isinstance(normalized, dict):
        raise TypeError(f"managed job {label} must be a JSON object")
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MANAGED_STATE_MAX_BYTES:
        raise ValueError(
            f"managed job {label} exceeds {MANAGED_STATE_MAX_BYTES} bytes"
        )
    return normalized
