"""Shell-backed tracked-job lifecycle operations."""

import contextlib
from collections.abc import Mapping
from typing import Any

from ...audit import audit
from ...errors import public_error_type
from ...jobs import status as job_status
from ...jobs.persistence import (
    TERMINAL_STATUSES,
)
from ...jobs.persistence import (
    prune_store as _prune_store,
)
from ...jobs.persistence import (
    remove_attempt_files as _remove_attempt_files,
)
from ...jobs.persistence import (
    remove_attempt_paths as _remove_attempt_paths,
)
from ...jobs.recovery import store_transaction as _store_transaction
from ...jobs.state import (
    ACTIVE_STATUSES,
    JobAttemptPaths,
    JobRow,
    JobStatusPayload,
    MutableJobRow,
)
from ...jobs.state import (
    begin_job_operation as _begin_job_operation,
)
from ...jobs.state import (
    clear_job_operation as _clear_job_operation,
)
from ...jobs.state import (
    discard_job_operation as _discard_job_operation,
)
from ...jobs.state import (
    find_session_job as _find_session_job,
)
from ...jobs.state import (
    job_operation_matches as _job_operation_matches,
)
from ...jobs.state import (
    job_shell_id as _job_shell_id,
)
from ...jobs.state import (
    new_job_id as _new_job_id,
)
from ...jobs.state import (
    public_job as _public_job,
)
from ...jobs.state import (
    utc as _utc,
)
from ...schemas.result_models.jobs import (
    JobListOutput,
    JobRetryOutput,
    JobStartOutput,
    JobStopOutput,
    JobTailOutput,
)
from ..config import ExecutorConfig
from ..shell import (
    authoritative_persistent_shell_ids_execute,
    kill_persistent_shell_execute,
    read_persistent_shell_output_execute,
    start_persistent_shell_execute,
)
from ..tool_session.lifecycle import session_lifecycle_lock
from ..tool_session.store import ToolSessionStore
from . import lifecycle as job_lifecycle

_shell_safe_name = job_lifecycle._shell_safe_name
_prepare_attempt = job_lifecycle._prepare_attempt
_clear_pending_retry = job_lifecycle._clear_pending_retry
_adopt_pending_retry = job_lifecycle._adopt_pending_retry


def _managed_job_has_local_task(_job: Mapping[str, Any]) -> bool:
    """Reject accidental managed-row reconciliation in the shell backend."""
    return False


def _managed_job_liveness(_job: Mapping[str, Any]) -> str:
    """Return a terminal fallback that is unreachable for shell-only callers."""
    return "dead"


def _read_status(job: Mapping[str, Any]) -> JobStatusPayload | None:
    """Read one shell attempt status through a patchable backend seam."""
    return job_lifecycle._read_status(job)


def _read_status_path(raw_path: Any) -> JobStatusPayload | None:
    """Read one explicit shell status path through a patchable backend seam."""
    return job_lifecycle._read_status_path(raw_path)


def _refresh_job_status(
    job: MutableJobRow,
    active_shells: set[str] | None,
    now: float | None = None,
) -> MutableJobRow:
    """Reconcile one shell row without importing the managed backend."""
    if str(job.get("kind") or "shell") == "managed":
        raise RuntimeError("managed job passed to shell job lifecycle")
    return job_lifecycle._refresh_job_status(
        job,
        active_shells,
        now,
        managed_job_has_local_task=_managed_job_has_local_task,
        managed_job_liveness=_managed_job_liveness,
        read_status=_read_status,
        read_status_path=_read_status_path,
    )


async def list_shell_jobs_unlocked(
    config: ExecutorConfig,
    session_store: ToolSessionStore,
    session_id: str,
    include_finished: bool = True,
) -> JobListOutput:
    """List shell-backed jobs while the owner session lifecycle lock is held."""
    session_store.touch_session(session_id)
    active = await authoritative_persistent_shell_ids_execute(
        config, session_store
    )
    now = _utc()
    with _store_transaction() as store:
        jobs = store.get("jobs", [])
        if not isinstance(jobs, list):
            jobs = []
            store["jobs"] = jobs
        owned: list[MutableJobRow] = []
        for row in jobs:
            if not isinstance(row, dict):
                continue
            if str(row.get("kind") or "shell") == "managed":
                continue
            if str(row.get("session_id") or "") != session_id:
                continue
            _refresh_job_status(row, active, now)
            owned.append(row)
        _prune_store(store, max_jobs=config.max_jobs)
        rows = [
            _public_job(row)
            for row in owned
            if include_finished or row.get("status") not in TERMINAL_STATUSES
        ]
        counts: dict[str, int] = {}
        for row in owned:
            status = str(row.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
    rows.sort(key=lambda item: item.created_at, reverse=True)
    return JobListOutput(jobs=rows, counts=counts)


async def tail_shell_job_unlocked(
    config: ExecutorConfig,
    session_store: ToolSessionStore,
    session_id: str,
    job_id: str,
    lines: int = 200,
) -> JobTailOutput:
    """Read one shell-backed job while the owner session lifecycle lock is held."""
    session_store.touch_session(session_id)
    active = await authoritative_persistent_shell_ids_execute(
        config, session_store
    )
    with _store_transaction() as store:
        job = _find_session_job(store, session_id, job_id)
        if str(job.get("kind") or "shell") == "managed":
            raise RuntimeError(f"job is not shell-backed: {job_id}")
        job = _refresh_job_status(job, active)
        public = _public_job(job)
        log_path = str(job.get("log_path") or "")
        shell_id = _job_shell_id(job)
        status = str(job.get("status") or "unknown")

    output = job_status._read_log_tail(
        log_path,
        lines,
        max_bytes=max(1, config.max_job_log_bytes),
    )
    if not output and status in ACTIVE_STATUSES and shell_id:
        try:
            tail = await read_persistent_shell_output_execute(
                config, shell_id, lines
            )
            output = str(tail.model_dump().get("output", ""))
        except Exception as exc:
            if active is not None:
                with _store_transaction() as store:
                    current = _find_session_job(store, session_id, job_id)
                    if (
                        current.get("status") == "running"
                        and _job_shell_id(current) == shell_id
                    ):
                        completed = _utc()
                        if shell_id in active:
                            current.update(
                                {
                                    "updated_at": completed,
                                    "error": (
                                        "persistent shell output capture failed: "
                                        f"{exc}"
                                    ),
                                }
                            )
                        else:
                            current.update(
                                {
                                    "status": "lost",
                                    "updated_at": completed,
                                    "completed_at": completed,
                                    "error": str(exc),
                                    "shell_absence_confirmed": True,
                                }
                            )
                    public = _public_job(current)

    message = None
    if public.status in TERMINAL_STATUSES:
        message = (
            f"job completed with exit code {public.exit_code}"
            if public.exit_code is not None
            else f"job is {public.status}"
        )
    return JobTailOutput(job=public, output=output, message=message)


async def start_shell_job_unlocked(
    config: ExecutorConfig,
    session_store: ToolSessionStore,
    session_id: str,
    command: str,
    cwd: str = ".",
    name: str | None = None,
) -> JobStartOutput:
    """Start one durable shell-backed job while its session lock is held."""
    session = session_store.touch_session(session_id)
    resolved_cwd = session_store.resolve_session_path(
        session, cwd, must_exist=True
    )
    job_id = _new_job_id()
    display_name = name or job_id
    shell_name = _shell_safe_name(f"{display_name}-{job_id}")
    paths, runner_command = _prepare_attempt(
        config, job_id, 1, command, resolved_cwd
    )
    now = _utc()
    active_shells = await authoritative_persistent_shell_ids_execute(
        config, session_store
    )
    job: JobRow = {
        "job_id": job_id,
        "kind": "shell",
        "name": display_name,
        "status": "starting",
        "command": command,
        "cwd": str(resolved_cwd),
        "session_id": session_id,
        "shell_id": shell_name,
        "backend": None,
        "command_path": str(paths["command"]),
        "log_path": str(paths["log"]),
        "status_path": str(paths["status"]),
        "created_at": now,
        "updated_at": now,
        "last_started_at": None,
        "completed_at": None,
        "exit_code": None,
        "error": None,
        "log_truncated": False,
        "output_bytes": 0,
        "attempts": 1,
    }
    operation_id = _begin_job_operation(job, "start")
    try:
        with _store_transaction() as store:
            retained = []
            for row in store.get("jobs", []):
                if not isinstance(row, dict):
                    continue
                if (
                    str(row.get("session_id") or "") == session_id
                    and str(row.get("kind") or "shell") != "managed"
                ):
                    _refresh_job_status(row, active_shells, now)
                retained.append(row)
            store["jobs"] = retained
            _prune_store(store, max_jobs=config.max_jobs)
            store["jobs"].append(job)
    except BaseException:
        _discard_job_operation(operation_id)
        _remove_attempt_paths(paths)
        raise

    try:
        try:
            shell = await start_persistent_shell_execute(
                config,
                session_store,
                str(resolved_cwd),
                shell_name,
                runner_command,
                owner_session_id=session_id,
            )
        except Exception as exc:
            _remove_attempt_paths(paths)
            with _store_transaction() as store:
                current = _find_session_job(store, session_id, job_id)
                if current.get(
                    "status"
                ) == "starting" and _job_operation_matches(
                    current, operation_id
                ):
                    _clear_job_operation(current)
                    completed = _utc()
                    current.update(
                        {
                            "status": "failed",
                            "updated_at": completed,
                            "completed_at": completed,
                            "error": f"start failed: {public_error_type(exc)}: {exc}",
                        }
                    )
            raise

        shell_data = shell.model_dump()
        changed_while_starting = False
        try:
            with _store_transaction() as store:
                current = _find_session_job(store, session_id, job_id)
                if current.get(
                    "status"
                ) != "starting" or not _job_operation_matches(
                    current, operation_id
                ):
                    changed_while_starting = True
                else:
                    started_at = _utc()
                    _clear_job_operation(current)
                    current.update(
                        {
                            "status": "running",
                            "shell_id": shell_data["shell_id"],
                            "backend": shell_data.get("backend"),
                            "updated_at": started_at,
                            "last_started_at": started_at,
                            "error": None,
                        }
                    )
                public = _public_job(current)
        except Exception:
            with contextlib.suppress(Exception):
                await kill_persistent_shell_execute(
                    config, session_store, shell_data["shell_id"]
                )
            _remove_attempt_paths(paths)
            raise
        if changed_while_starting:
            with contextlib.suppress(Exception):
                await kill_persistent_shell_execute(
                    config, session_store, shell_data["shell_id"]
                )
            _remove_attempt_paths(paths)
            raise RuntimeError(f"job changed while starting: {job_id}")
        audit(
            "job_start",
            job_id=job_id,
            session=session_id,
            shell_id=shell_data["shell_id"],
            cwd=str(resolved_cwd),
            command=command,
        )
        return JobStartOutput(**public.model_dump())
    finally:
        _discard_job_operation(operation_id)


async def reconcile_shell_jobs_execute(
    config: ExecutorConfig, session_store: ToolSessionStore
) -> bool:
    """Reconcile durable shell-job state and authoritative shell membership."""
    with _store_transaction() as store:
        jobs = store.get("jobs", [])
        if not isinstance(jobs, list):
            return True
        owner_session_ids = sorted(
            {
                str(row.get("session_id") or "")
                for row in jobs
                if isinstance(row, dict)
                and str(row.get("kind") or "shell") != "managed"
                and row.get("session_id")
            }
        )

    inventory_authoritative = True
    for session_id in owner_session_ids:
        async with session_lifecycle_lock(session_id):
            active_shells = await authoritative_persistent_shell_ids_execute(
                config, session_store
            )
            if active_shells is None:
                inventory_authoritative = False
            now = _utc()
            with _store_transaction() as store:
                jobs = store.get("jobs", [])
                if not isinstance(jobs, list):
                    continue
                for row in jobs:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("kind") or "shell") == "managed":
                        continue
                    if str(row.get("session_id") or "") != session_id:
                        continue
                    _refresh_job_status(row, active_shells, now)

    active_shells = await authoritative_persistent_shell_ids_execute(
        config, session_store
    )
    if active_shells is None:
        inventory_authoritative = False
    now = _utc()
    with _store_transaction() as store:
        jobs = store.get("jobs", [])
        if isinstance(jobs, list):
            for row in jobs:
                if not isinstance(row, dict):
                    continue
                if str(row.get("kind") or "shell") == "managed":
                    continue
                if row.get("session_id"):
                    continue
                _refresh_job_status(row, active_shells, now)
        _prune_store(store, max_jobs=config.max_jobs)
    return inventory_authoritative


async def stop_shell_job_unlocked(
    config: ExecutorConfig,
    session_store: ToolSessionStore,
    session_id: str,
    job_id: str,
) -> JobStopOutput:
    """Stop one shell-backed job while its owner session lock is held."""
    active = await authoritative_persistent_shell_ids_execute(
        config, session_store
    )
    operation_id = ""
    shell_id = ""
    try:
        with _store_transaction() as store:
            job = _find_session_job(store, session_id, job_id)
            if str(job.get("kind") or "shell") == "managed":
                raise RuntimeError(f"job is not shell-backed: {job_id}")
            job = _refresh_job_status(job, active)
            shell_id = _job_shell_id(job)
            if job.get("status") == "lost":
                if active is None:
                    return JobStopOutput(
                        job=_public_job(job), killed=False, stderr=""
                    )
                if shell_id not in active:
                    completed = _utc()
                    job.update(
                        {
                            "status": "stopped",
                            "updated_at": completed,
                            "completed_at": completed,
                            "exit_code": None,
                            "error": None,
                        }
                    )
                    return JobStopOutput(
                        job=_public_job(job), killed=False, stderr=""
                    )
                job.update(
                    {
                        "status": "running",
                        "updated_at": _utc(),
                        "completed_at": None,
                    }
                )
            if job.get("status") != "running":
                return JobStopOutput(
                    job=_public_job(job), killed=False, stderr=""
                )
            job["status"] = "stopping"
            job["updated_at"] = _utc()
            operation_id = _begin_job_operation(job, "stop")
    except BaseException:
        _discard_job_operation(operation_id)
        raise

    try:
        try:
            result = await kill_persistent_shell_execute(
                config, session_store, shell_id
            )
        except Exception as exc:
            still_active = True
            active_after_failure = (
                await authoritative_persistent_shell_ids_execute(
                    config, session_store
                )
            )
            if active_after_failure is not None:
                still_active = shell_id in active_after_failure
            with _store_transaction() as store:
                job = _find_session_job(store, session_id, job_id)
                if (
                    job.get("status") == "stopping"
                    and _job_shell_id(job) == shell_id
                    and _job_operation_matches(job, operation_id)
                ):
                    completed = _utc()
                    job["status"] = "running" if still_active else "stopped"
                    job["updated_at"] = completed
                    job["error"] = (
                        f"stop failed: {public_error_type(exc)}: {exc}"
                    )
                    if not still_active:
                        job["completed_at"] = completed
                    else:
                        job["completed_at"] = None
                    _clear_job_operation(job)
            raise

        data = result.model_dump()
        killed = bool(data.get("killed"))
        stderr = str(data.get("stderr") or "")
        active_after_stop: set[str] | None = None
        if not killed:
            active_after_stop = (
                await authoritative_persistent_shell_ids_execute(
                    config, session_store
                )
            )
        with _store_transaction() as store:
            job = _find_session_job(store, session_id, job_id)
            if (
                job.get("status") == "stopping"
                and _job_shell_id(job) == shell_id
                and _job_operation_matches(job, operation_id)
            ):
                completed = _utc()
                if killed:
                    status = "stopped"
                elif active_after_stop is None or shell_id in active_after_stop:
                    status = "running"
                else:
                    status = "stopped"
                job.update(
                    {
                        "status": status,
                        "updated_at": completed,
                        "completed_at": completed
                        if status == "stopped"
                        else None,
                        "exit_code": None,
                        "error": None
                        if status == "stopped"
                        else stderr or "shell was not stopped",
                    }
                )
                _clear_job_operation(job)
            public = _public_job(job)
        audit(
            "job_stop",
            job_id=job_id,
            session=session_id,
            shell_id=shell_id,
            killed=killed,
        )
        return JobStopOutput(job=public, killed=killed, stderr=stderr)
    finally:
        _discard_job_operation(operation_id)


async def retry_shell_job_unlocked(
    config: ExecutorConfig,
    session_store: ToolSessionStore,
    session_id: str,
    job_id: str,
) -> JobRetryOutput:
    """Retry one terminal shell-backed job while lifecycle locks are held."""
    session = session_store.touch_session(session_id)
    active = await authoritative_persistent_shell_ids_execute(
        config, session_store
    )
    operation_id = ""
    try:
        with _store_transaction() as store:
            job = _find_session_job(store, session_id, job_id)
            if str(job.get("kind") or "shell") == "managed":
                raise RuntimeError(f"job is not shell-backed: {job_id}")
            job = _refresh_job_status(job, active)
            if job.get("status") in ACTIVE_STATUSES:
                raise RuntimeError(f"job is still active: {job_id}")
            attempts = int(job.get("attempts") or 1) + 1
            command = str(job.get("command") or "")
            resolved_cwd = session_store.resolve_session_path(
                session, str(job.get("cwd") or "."), must_exist=True
            )
            display_name = str(job.get("name") or job_id)
            shell_name = _shell_safe_name(f"{display_name}-{job_id}-{attempts}")
            job.update(
                {
                    "status": "retrying",
                    "updated_at": _utc(),
                    "pending_attempt": attempts,
                    "pending_shell_id": shell_name,
                }
            )
            operation_id = _begin_job_operation(job, "retry")
    except BaseException:
        _discard_job_operation(operation_id)
        raise

    paths: JobAttemptPaths | None = None
    try:
        try:
            paths, runner_command = _prepare_attempt(
                config, job_id, attempts, command, resolved_cwd
            )
            with _store_transaction() as store:
                current = _find_session_job(store, session_id, job_id)
                if current.get(
                    "status"
                ) != "retrying" or not _job_operation_matches(
                    current, operation_id
                ):
                    raise RuntimeError(
                        f"job changed while preparing retry: {job_id}"
                    )
                current.update(
                    {
                        "pending_command_path": str(paths["command"]),
                        "pending_log_path": str(paths["log"]),
                        "pending_status_path": str(paths["status"]),
                    }
                )
            shell = await start_persistent_shell_execute(
                config,
                session_store,
                str(resolved_cwd),
                shell_name,
                runner_command,
                owner_session_id=session_id,
            )
        except Exception as exc:
            _remove_attempt_paths(paths)
            with _store_transaction() as store:
                current = _find_session_job(store, session_id, job_id)
                if current.get(
                    "status"
                ) == "retrying" and _job_operation_matches(
                    current, operation_id
                ):
                    pending_attempt = current.get("pending_attempt")
                    if pending_attempt is not None:
                        current["attempts"] = int(pending_attempt)
                    _clear_pending_retry(current)
                    _clear_job_operation(current)
                    completed = _utc()
                    current.update(
                        {
                            "status": "failed",
                            "updated_at": completed,
                            "completed_at": completed,
                            "exit_code": None,
                            "error": f"retry failed: {public_error_type(exc)}: {exc}",
                        }
                    )
            raise

        shell_data = shell.model_dump()
        changed_while_retrying = False
        try:
            with _store_transaction() as store:
                current = _find_session_job(store, session_id, job_id)
                if current.get(
                    "status"
                ) != "retrying" or not _job_operation_matches(
                    current, operation_id
                ):
                    changed_while_retrying = True
                else:
                    _adopt_pending_retry(current)
                    _clear_pending_retry(current)
                    _clear_job_operation(current)
                    started_at = _utc()
                    current.update(
                        {
                            "status": "running",
                            "cwd": str(resolved_cwd),
                            "shell_id": shell_data["shell_id"],
                            "backend": shell_data.get("backend"),
                            "updated_at": started_at,
                            "last_started_at": started_at,
                            "completed_at": None,
                            "exit_code": None,
                            "error": None,
                            "log_truncated": False,
                            "output_bytes": 0,
                        }
                    )
                public = _public_job(current)
        except Exception:
            with contextlib.suppress(Exception):
                await kill_persistent_shell_execute(
                    config, session_store, shell_data["shell_id"]
                )
            _remove_attempt_paths(paths)
            raise
        if changed_while_retrying:
            with contextlib.suppress(Exception):
                await kill_persistent_shell_execute(
                    config, session_store, shell_data["shell_id"]
                )
            _remove_attempt_paths(paths)
            raise RuntimeError(f"job changed while retrying: {job_id}")
        _remove_attempt_files(job_id, keep_attempt=attempts)
        audit(
            "job_retry",
            job_id=job_id,
            session=session_id,
            shell_id=shell_data["shell_id"],
            attempts=attempts,
        )
        return JobRetryOutput(**public.model_dump())
    finally:
        _discard_job_operation(operation_id)
