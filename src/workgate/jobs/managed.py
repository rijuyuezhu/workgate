"""Managed tracked-job handlers, tasks, leases, and lifecycle operations."""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from ..audit import audit
from ..config.settings import get_settings
from ..errors import public_error_type
from ..schemas.result_models.jobs import (
    JobListOutput,
    JobRetryOutput,
    JobStartOutput,
    JobStopOutput,
)
from ..tool_session.lifecycle import session_lifecycle_lock
from ..tool_session.store import get_tool_session_store
from ..utils.private_files import write_private_text
from ..utils.runtime_identity import (
    MANAGED_JOB_LEASE_VERSION,
    PROCESS_INSTANCE_ID,
    ManagedJobLease,
    managed_job_lease_state,
)
from . import lifecycle as job_lifecycle
from . import recovery as job_recovery
from .persistence import (
    TERMINAL_STATUSES,
)
from .persistence import (
    attempt_paths as _attempt_paths,
)
from .persistence import (
    prune_store as _prune_store,
)
from .persistence import (
    remove_attempt_files as _remove_attempt_files,
)
from .persistence import (
    remove_attempt_paths as _remove_attempt_paths,
)
from .runner import compact_log as _compact_log
from .state import (
    ACTIVE_STATUSES,
    CONFIRMED_TERMINAL_STATUSES,
    JobAttemptPaths,
    JobRow,
    MutableJobRow,
)
from .state import (
    begin_job_operation as _begin_job_operation,
)
from .state import (
    clear_job_operation as _clear_job_operation,
)
from .state import (
    discard_job_operation as _discard_job_operation,
)
from .state import (
    find_session_job as _find_session_job,
)
from .state import (
    job_agent_session_id as _job_agent_session_id,
)
from .state import (
    job_operation_matches as _job_operation_matches,
)
from .state import (
    managed_json_dict as _managed_json_dict,
)
from .state import (
    new_job_id as _new_job_id,
)
from .state import (
    public_job as _public_job,
)
from .state import (
    utc as _utc,
)

MANAGED_JOB_STORE_RETRY_ATTEMPTS = 2
JOB_STORE_LOCK_RETRY_INTERVAL_S = job_recovery.JOB_STORE_LOCK_RETRY_INTERVAL_S
_store_transaction = job_recovery.store_transaction
_write_managed_deferred_update = job_recovery.write_managed_deferred_update
_apply_managed_update = job_recovery.apply_managed_update
_clear_pending_retry = job_lifecycle._clear_pending_retry
type ManagedJobHandler = Callable[
    [ManagedJobContext, dict[str, Any]], Awaitable[dict[str, Any] | None]
]


class ManagedJobsRuntime:
    """Own process-local managed-job handlers, tasks, and liveness leases."""

    def __init__(self) -> None:
        self.handlers: dict[str, ManagedJobHandler] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.leases: dict[str, ManagedJobLease] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._admitting = False
        self._closed = False

    def register_handler(self, kind: str, handler: ManagedJobHandler) -> None:
        """Register one handler on this owner without creating live state."""
        if self._closed:
            raise RuntimeError("managed jobs runtime is closed")
        normalized = kind.strip()
        if not normalized:
            raise ValueError("managed job kind must not be empty")
        existing = self.handlers.get(normalized)
        if existing is not None and existing is not handler:
            raise ValueError(
                f"managed job handler already registered: {normalized}"
            )
        self.handlers[normalized] = handler

    async def start(self) -> None:
        """Bind this owner to one event loop and begin admitting new work."""
        if self._closed:
            raise RuntimeError(
                "ManagedJobsRuntime cannot be restarted after close"
            )
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("ManagedJobsRuntime cannot span event loops")
        self._started = True
        self._admitting = True

    def require_admission(self) -> None:
        """Reject new managed starts/retries outside the owning lifespan."""
        if not self._started or not self._admitting or self._closed:
            raise RuntimeError("managed jobs runtime is not accepting new work")

    def has_local_task(self, job: Mapping[str, Any]) -> bool:
        """Return whether this owner still runs the managed task for one row."""
        if str(job.get("runtime_instance_id") or "") != PROCESS_INSTANCE_ID:
            return False
        task = self.tasks.get(str(job.get("job_id") or ""))
        return task is not None and not task.done()

    def acquire_lease(self, job_id: str) -> ManagedJobLease:
        """Acquire and retain one cross-process liveness lease."""
        if job_id in self.leases:
            raise RuntimeError(f"managed job lease is already held: {job_id}")
        lease = ManagedJobLease(job_id)
        lease.acquire()
        self.leases[job_id] = lease
        return lease

    def release_lease(self, job_id: str) -> None:
        """Release one retained lease after terminal state is durable."""
        lease = self.leases.pop(job_id, None)
        if lease is not None:
            lease.release()

    async def aclose(self) -> None:
        """Stop admission, cancel owned tasks, and release every retained lease."""
        if self._closed:
            return
        if (
            self._loop is not None
            and asyncio.get_running_loop() is not self._loop
        ):
            raise RuntimeError(
                "ManagedJobsRuntime must close on its owning event loop"
            )
        self._admitting = False
        self._closed = True
        tasks = tuple(self.tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        results = (
            await asyncio.gather(*tasks, return_exceptions=True)
            if tasks
            else ()
        )
        self.tasks.clear()

        lease_error: BaseException | None = None
        for job_id in tuple(self.leases):
            try:
                self.release_lease(job_id)
            except BaseException as exc:
                if lease_error is None:
                    lease_error = exc

        task_error = next(
            (
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            ),
            None,
        )
        if task_error is not None:
            raise task_error
        if lease_error is not None:
            raise lease_error


_MANAGED_JOBS_RUNTIME: ManagedJobsRuntime | None = None


def configure_managed_jobs_runtime(
    runtime: ManagedJobsRuntime | None,
) -> ManagedJobsRuntime | None:
    """Install a non-owning compatibility binding and return the prior owner."""
    global _MANAGED_JOBS_RUNTIME
    previous = _MANAGED_JOBS_RUNTIME
    _MANAGED_JOBS_RUNTIME = runtime
    return previous


def managed_jobs_runtime() -> ManagedJobsRuntime:
    """Return the controller-owned managed Jobs runtime compatibility binding."""
    if _MANAGED_JOBS_RUNTIME is None:
        raise RuntimeError(
            "managed jobs runtime is not configured; start ControlRuntime"
        )
    return _MANAGED_JOBS_RUNTIME


def _managed_job_liveness(job: Mapping[str, Any]) -> str:
    """Return the authoritative cross-process liveness state for a managed row."""
    return managed_job_lease_state(
        str(job.get("job_id") or ""),
        job.get("managed_lease_version"),
    )


def _managed_job_has_local_task(job: Mapping[str, Any]) -> bool:
    """Return whether the configured owner runs one still-live managed task."""
    return managed_jobs_runtime().has_local_task(job)


def _refresh_job_status(
    job: MutableJobRow,
    active_shells: set[str] | None,
    now: float | None = None,
) -> MutableJobRow:
    """Reconcile one row using this runtime's authoritative managed liveness."""
    return job_lifecycle._refresh_job_status(
        job,
        active_shells,
        now,
        managed_job_has_local_task=_managed_job_has_local_task,
        managed_job_liveness=_managed_job_liveness,
    )


def register_managed_job_handler(kind: str, handler: ManagedJobHandler) -> None:
    """Register a handler on the configured controller-owned Jobs runtime."""
    managed_jobs_runtime().register_handler(kind, handler)


def _managed_store_update(
    operation: str,
    session_id: str,
    job_id: str,
    payload: dict[str, Any],
) -> None:
    """Apply one update after bounded retries or durably defer it in order."""
    for attempt in range(1, MANAGED_JOB_STORE_RETRY_ATTEMPTS + 1):
        try:
            with _store_transaction() as store:
                try:
                    job = _find_session_job(store, session_id, job_id)
                except KeyError:
                    if operation == "append_log":
                        return
                    raise
                _apply_managed_update(job, operation, payload)
            return
        except TimeoutError:
            if attempt == MANAGED_JOB_STORE_RETRY_ATTEMPTS:
                break
            audit(
                "managed_job_store_update_retry",
                session=session_id,
                job_id=job_id,
                operation=operation,
                attempt=attempt,
            )
            time.sleep(JOB_STORE_LOCK_RETRY_INTERVAL_S)
    _write_managed_deferred_update(session_id, job_id, operation, payload)


def _append_managed_log(
    session_id: str, job_id: str, path: str, message: str
) -> None:
    """Append one bounded line and durably update or journal output metadata."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = message if message.endswith("\n") else message + "\n"
    encoded = payload.encode("utf-8", errors="replace")
    max_bytes = max(1, int(get_settings().max_job_log_bytes))
    with target.open("a+b") as handle:
        with contextlib.suppress(OSError):
            target.chmod(0o600)
        handle.write(encoded)
        handle.flush()
        truncated = _compact_log(handle, max_bytes)
    _managed_store_update(
        "append_log",
        session_id,
        job_id,
        {
            "bytes": len(encoded),
            "truncated": truncated,
            "updated_at": _utc(),
        },
    )


def _update_managed_progress(
    session_id: str, job_id: str, progress: dict[str, Any]
) -> None:
    """Persist or journal one bounded managed-job progress snapshot."""
    normalized = _managed_json_dict(progress, label="progress")
    _managed_store_update(
        "update_progress",
        session_id,
        job_id,
        {"progress": normalized, "updated_at": _utc()},
    )


class ManagedJobContext:
    """Durable logging and progress interface supplied to managed-job handlers."""

    def __init__(self, session_id: str, job_id: str, log_path: str) -> None:
        self.session_id = session_id
        self.job_id = job_id
        self.log_path = log_path

    async def log(self, message: str) -> None:
        """Append one message to the bounded durable job log."""
        await asyncio.to_thread(
            _append_managed_log,
            self.session_id,
            self.job_id,
            self.log_path,
            str(message),
        )

    async def update_progress(self, **progress: Any) -> None:
        """Replace the durable structured progress snapshot."""
        await asyncio.to_thread(
            _update_managed_progress,
            self.session_id,
            self.job_id,
            progress,
        )


def _finish_managed_job(
    session_id: str,
    job_id: str,
    *,
    status: str,
    exit_code: int | None,
    error: str | None,
    result: dict[str, Any] | None = None,
) -> None:
    """Commit one terminal managed-job state without overwriting a completed row."""
    normalized_result = (
        _managed_json_dict(result, label="result")
        if result is not None
        else None
    )
    _managed_store_update(
        "finish",
        session_id,
        job_id,
        {
            "status": status,
            "completed_at": _utc(),
            "exit_code": exit_code,
            "error": error,
            "has_result": normalized_result is not None,
            "result": normalized_result,
        },
    )


async def _run_managed_job(
    runtime: ManagedJobsRuntime,
    session_id: str,
    job_id: str,
    kind: str,
    payload: dict[str, Any],
    log_path: str,
) -> None:
    """Run one registered managed handler and durably record its terminal state."""
    context = ManagedJobContext(session_id, job_id, log_path)
    handler = runtime.handlers[kind]
    try:
        result = await handler(context, dict(payload))
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await context.log("job cancelled")
        await asyncio.to_thread(
            _finish_managed_job,
            session_id,
            job_id,
            status="stopped",
            exit_code=None,
            error=None,
        )
        raise
    except Exception as exc:
        error = f"{public_error_type(exc)}: {exc}"
        with contextlib.suppress(Exception):
            await context.log(error)
        await asyncio.to_thread(
            _finish_managed_job,
            session_id,
            job_id,
            status="failed",
            exit_code=1,
            error=error,
        )
    else:
        try:
            await asyncio.to_thread(
                _finish_managed_job,
                session_id,
                job_id,
                status="succeeded",
                exit_code=0,
                error=None,
                result=result,
            )
        except Exception as exc:
            error = f"{public_error_type(exc)}: {exc}"
            with contextlib.suppress(Exception):
                await context.log(error)
            await asyncio.to_thread(
                _finish_managed_job,
                session_id,
                job_id,
                status="failed",
                exit_code=1,
                error=error,
            )
    finally:
        current = asyncio.current_task()
        if runtime.tasks.get(job_id) is current:
            runtime.tasks.pop(job_id, None)
        runtime.release_lease(job_id)


def _launch_managed_job(
    runtime: ManagedJobsRuntime,
    session_id: str,
    job_id: str,
    kind: str,
    payload: dict[str, Any],
    log_path: str,
) -> None:
    """Create and retain one process-local managed-job task."""
    runtime.require_admission()
    acquired_here = False
    if job_id not in runtime.leases:
        runtime.acquire_lease(job_id)
        acquired_here = True
    try:
        task = asyncio.create_task(
            _run_managed_job(
                runtime, session_id, job_id, kind, payload, log_path
            ),
            name=f"managed-job-{job_id}",
        )
        runtime.tasks[job_id] = task
    except BaseException:
        if acquired_here:
            runtime.release_lease(job_id)
        raise


async def start_managed_job(
    session_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    name: str | None = None,
    command: str | None = None,
    cwd: str = ".",
) -> JobStartOutput:
    """Start one controller-managed task under session lifecycle admission."""
    async with session_lifecycle_lock(session_id):
        return await _start_managed_job_unlocked(
            session_id,
            kind,
            payload,
            name=name,
            command=command,
            cwd=cwd,
        )


async def _start_managed_job_unlocked(
    session_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    name: str | None = None,
    command: str | None = None,
    cwd: str = ".",
) -> JobStartOutput:
    """Start one controller-managed task owned by an explicit agent session."""
    runtime = managed_jobs_runtime()
    runtime.require_admission()
    get_tool_session_store().touch_session(session_id)
    normalized_kind = kind.strip()
    if normalized_kind not in runtime.handlers:
        raise ValueError(f"unknown managed job kind: {normalized_kind}")
    normalized_payload = _managed_json_dict(payload, label="payload")
    job_id = _new_job_id()
    attempt = 1
    paths = _attempt_paths(job_id, attempt)
    write_private_text(paths["log"], "")
    now = _utc()
    display_name = name or f"{normalized_kind}-{job_id}"
    job: JobRow = {
        "job_id": job_id,
        "kind": "managed",
        "managed_kind": normalized_kind,
        "managed_payload": normalized_payload,
        "runtime_instance_id": PROCESS_INSTANCE_ID,
        "managed_lease_version": MANAGED_JOB_LEASE_VERSION,
        "name": display_name,
        "status": "running",
        "command": command or normalized_kind,
        "cwd": cwd,
        "session_id": session_id,
        "shell_id": None,
        "backend": "managed",
        "command_path": None,
        "log_path": str(paths["log"]),
        "status_path": None,
        "created_at": now,
        "updated_at": now,
        "last_started_at": now,
        "completed_at": None,
        "exit_code": None,
        "error": None,
        "log_truncated": False,
        "output_bytes": 0,
        "attempts": attempt,
        "progress": None,
        "result": None,
    }
    try:
        runtime.acquire_lease(job_id)
        with _store_transaction() as store:
            store["jobs"].append(job)
        _launch_managed_job(
            runtime,
            session_id,
            job_id,
            normalized_kind,
            normalized_payload,
            str(paths["log"]),
        )
    except BaseException:
        runtime.release_lease(job_id)
        _remove_attempt_paths(paths)
        with contextlib.suppress(Exception), _store_transaction() as store:
            store["jobs"] = [
                row
                for row in store.get("jobs", [])
                if row.get("job_id") != job_id
            ]
        raise
    audit(
        "job_start",
        job_id=job_id,
        session=session_id,
        backend="managed",
        kind=normalized_kind,
    )
    return JobStartOutput(**_public_job(job).model_dump())


async def _stop_managed_job(
    session_id: str,
    job_id: str,
    operation_id: str,
) -> JobStopOutput:
    """Cancel one active process-local managed task and retain its durable log."""
    runtime = managed_jobs_runtime()
    task = runtime.tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    with _store_transaction() as store:
        job = _find_session_job(store, session_id, job_id)
        if job.get("status") == "stopping" and _job_operation_matches(
            job, operation_id
        ):
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
            _clear_job_operation(job)
        public = _public_job(job)
    killed = public.status == "stopped"
    audit(
        "job_stop",
        job_id=job_id,
        session=session_id,
        backend="managed",
        killed=killed,
    )
    return JobStopOutput(job=public, killed=killed, stderr="")


async def _stop_managed_job_without_session_admission(
    session_id: str,
    job_id: str,
) -> JobStopOutput:
    """Stop one managed job when its owner session may already be terminating."""
    operation_id = ""
    try:
        with _store_transaction() as store:
            job = _refresh_job_status(
                _find_session_job(store, session_id, job_id), set()
            )
            if job.get("status") != "running":
                return JobStopOutput(
                    job=_public_job(job), killed=False, stderr=""
                )
            if str(job.get("kind") or "shell") != "managed":
                raise RuntimeError(f"job is not controller-managed: {job_id}")
            if not _managed_job_has_local_task(job):
                return JobStopOutput(
                    job=_public_job(job),
                    killed=False,
                    stderr=(
                        "managed job is owned by another live or uncertain runtime"
                    ),
                )
            job["status"] = "stopping"
            job["updated_at"] = _utc()
            operation_id = _begin_job_operation(job, "stop")
    except BaseException:
        _discard_job_operation(operation_id)
        raise
    try:
        return await _stop_managed_job(session_id, job_id, operation_id)
    finally:
        _discard_job_operation(operation_id)


async def job_stop_managed_references_execute(
    referenced_session_id: str,
    *,
    managed_kind: str,
    payload_key: str,
) -> list[str]:
    """Stop active managed jobs whose durable payload references one session."""
    candidates: list[tuple[str, str]] = []
    with _store_transaction() as store:
        for row in store.get("jobs", []):
            if not isinstance(row, dict):
                continue
            if str(row.get("kind") or "shell") != "managed":
                continue
            if str(row.get("managed_kind") or "") != managed_kind:
                continue
            payload = row.get("managed_payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get(payload_key) or "") != referenced_session_id:
                continue
            job = _refresh_job_status(row, set())
            if job.get("status") != "running":
                continue
            owner_session_id = str(job.get("session_id") or "")
            job_id = str(job.get("job_id") or "")
            if owner_session_id and job_id:
                candidates.append((owner_session_id, job_id))

    stopped: list[str] = []
    for owner_session_id, job_id in candidates:
        result = await _stop_managed_job_without_session_admission(
            owner_session_id, job_id
        )
        if result.killed or result.job.status in CONFIRMED_TERMINAL_STATUSES:
            stopped.append(job_id)
            continue
        raise RuntimeError(
            "managed job could not be confirmed stopped: "
            f"{job_id}: status={result.job.status!r}"
        )
    return stopped


async def _retry_managed_job(session_id: str, job_id: str) -> JobRetryOutput:
    """Relaunch one terminal managed operation from its durable handler payload."""
    runtime = managed_jobs_runtime()
    runtime.require_admission()
    operation_id = ""
    paths: JobAttemptPaths | None = None
    attempts = 0
    try:
        with _store_transaction() as store:
            job = _refresh_job_status(
                _find_session_job(store, session_id, job_id), set()
            )
            if job.get("status") in ACTIVE_STATUSES:
                raise RuntimeError(f"job is still active: {job_id}")
            kind = str(job.get("managed_kind") or "")
            if kind not in runtime.handlers:
                raise RuntimeError(
                    f"managed job handler is unavailable: {kind}"
                )
            payload = _managed_json_dict(
                job.get("managed_payload"), label="payload"
            )
            attempts = int(job.get("attempts") or 1) + 1
        runtime.acquire_lease(job_id)
        paths = _attempt_paths(job_id, attempts)
        write_private_text(paths["log"], "")
        with _store_transaction() as store:
            job = _refresh_job_status(
                _find_session_job(store, session_id, job_id), set()
            )
            if job.get("status") in ACTIVE_STATUSES:
                raise RuntimeError(f"job became active during retry: {job_id}")
            job.update(
                {
                    "status": "retrying",
                    "updated_at": _utc(),
                    "pending_attempt": attempts,
                    "pending_log_path": str(paths["log"]),
                    "progress": None,
                    "result": None,
                    "completed_at": None,
                    "exit_code": None,
                    "error": None,
                    "log_truncated": False,
                    "output_bytes": 0,
                    "runtime_instance_id": PROCESS_INSTANCE_ID,
                    "managed_lease_version": MANAGED_JOB_LEASE_VERSION,
                }
            )
            operation_id = _begin_job_operation(job, "retry")
        _launch_managed_job(
            runtime,
            session_id,
            job_id,
            kind,
            payload,
            str(paths["log"]),
        )
        with _store_transaction() as store:
            current = _find_session_job(store, session_id, job_id)
            if current.get("status") == "retrying" and _job_operation_matches(
                current, operation_id
            ):
                current.update(
                    {
                        "status": "running",
                        "attempts": attempts,
                        "log_path": str(paths["log"]),
                        "updated_at": _utc(),
                        "last_started_at": _utc(),
                    }
                )
                _clear_pending_retry(current)
                _clear_job_operation(current)
            public = _public_job(current)
        _remove_attempt_files(job_id, keep_attempt=attempts)
        audit(
            "job_retry",
            job_id=job_id,
            session=session_id,
            backend="managed",
            attempts=attempts,
        )
        return JobRetryOutput(**public.model_dump())
    except BaseException as exc:
        task = runtime.tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        runtime.release_lease(job_id)
        _remove_attempt_paths(paths)
        if operation_id:
            with contextlib.suppress(Exception), _store_transaction() as store:
                current = _find_session_job(store, session_id, job_id)
                if _job_operation_matches(current, operation_id):
                    completed = _utc()
                    current.update(
                        {
                            "status": "failed",
                            "attempts": attempts
                            or int(current.get("attempts") or 1),
                            "updated_at": completed,
                            "completed_at": completed,
                            "exit_code": None,
                            "error": f"retry failed: {public_error_type(exc)}: {exc}",
                        }
                    )
                    _clear_pending_retry(current)
                    _clear_job_operation(current)
        raise
    finally:
        _discard_job_operation(operation_id)


async def managed_job_list_execute(
    session_id: str, include_finished: bool
) -> JobListOutput:
    """List only controller-managed jobs owned by one explicit session."""
    get_tool_session_store().touch_session(session_id)
    now = _utc()
    with _store_transaction() as store:
        for row in store.get("jobs", []):
            if (
                _job_agent_session_id(row) == session_id
                and str(row.get("kind") or "shell") == "managed"
            ):
                _refresh_job_status(row, set(), now)
        _prune_store(store)
        owned = [
            row
            for row in store.get("jobs", [])
            if _job_agent_session_id(row) == session_id
            and str(row.get("kind") or "shell") == "managed"
        ]
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


def managed_job_id_set(session_id: str, job_ids: list[str]) -> set[str]:
    """Return requested identifiers owned by controller-managed jobs."""
    requested = set(job_ids)
    if not requested:
        return set()
    with _store_transaction() as store:
        return {
            str(row.get("job_id") or "")
            for row in store.get("jobs", [])
            if _job_agent_session_id(row) == session_id
            and str(row.get("kind") or "shell") == "managed"
            and str(row.get("job_id") or "") in requested
        }
