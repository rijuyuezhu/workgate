"""Durable job-store transactions and managed deferred-update recovery."""

import contextlib
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

from ..audit import audit
from ..persistence import get_state_store
from ..utils.private_files import atomic_write_private_text, private_file_lock
from .persistence import (
    job_store_lock_path as _job_store_lock_path,
)
from .persistence import (
    load_store as _load_store,
)
from .persistence import (
    save_store as _save_store,
)
from .state import (
    ACTIVE_STATUSES,
    JobStore,
    MutableJobRow,
    MutableJobStore,
)
from .state import (
    clear_job_operation as _clear_job_operation,
)
from .state import (
    find_session_job as _find_session_job,
)
from .state import (
    managed_json_dict as _managed_json_dict,
)
from .state import (
    utc as _utc,
)

JOB_STORE_LOCK_TIMEOUT_S = 2.0
JOB_STORE_LOCK_RETRY_INTERVAL_S = 0.05
MANAGED_DEFERRED_UPDATE_VERSION = 1
MANAGED_DEFERRED_APPLIED_KEY = "managed_deferred_update_ids"
MANAGED_DEFERRED_MAX_RECORD_BYTES = 524_288
_JOB_STORE_THREAD_LOCK = threading.RLock()
_MANAGED_DEFERRED_SEQUENCE_LOCK = threading.Lock()
_MANAGED_DEFERRED_NEXT_SEQUENCE: int | None = None


def managed_deferred_update_dir(*, create: bool = True) -> Path:
    """Return the private journal directory, creating it only for a writer."""
    path = get_state_store().layout.jobs_deferred_dir
    if create:
        path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            path.chmod(0o700)
    return path


def next_managed_deferred_sequence() -> int:
    """Allocate a process-safe sequence continuing any journal left by restart."""
    global _MANAGED_DEFERRED_NEXT_SEQUENCE
    with _MANAGED_DEFERRED_SEQUENCE_LOCK:
        if _MANAGED_DEFERRED_NEXT_SEQUENCE is None:
            maximum = -1
            directory = managed_deferred_update_dir(create=False)
            if directory.is_dir():
                for path in directory.glob("*.json"):
                    prefix = path.stem.split("-", 1)[0]
                    if prefix.isdigit():
                        maximum = max(maximum, int(prefix))
            _MANAGED_DEFERRED_NEXT_SEQUENCE = maximum + 1
        sequence = _MANAGED_DEFERRED_NEXT_SEQUENCE
        _MANAGED_DEFERRED_NEXT_SEQUENCE += 1
        return sequence


def write_managed_deferred_update(
    session_id: str,
    job_id: str,
    operation: str,
    payload: dict[str, Any],
) -> Path:
    """Atomically journal one managed update after bounded lock contention."""
    update_id = (
        f"{next_managed_deferred_sequence():020d}-"
        f"{time.time_ns():020d}-{uuid.uuid4().hex}"
    )
    path = managed_deferred_update_dir() / f"{update_id}.json"
    record = {
        "version": MANAGED_DEFERRED_UPDATE_VERSION,
        "update_id": update_id,
        "session_id": session_id,
        "job_id": job_id,
        "operation": operation,
        "payload": payload,
    }
    encoded = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MANAGED_DEFERRED_MAX_RECORD_BYTES:
        raise ValueError(
            "managed deferred update exceeds the private journal limit"
        )
    atomic_write_private_text(path, encoded)
    audit(
        "managed_job_store_update_deferred",
        session=session_id,
        job_id=job_id,
        operation=operation,
        update_id=update_id,
        path=str(path),
    )
    return path


def read_managed_deferred_record(path: Path) -> dict[str, Any]:
    """Read one bounded regular journal file without following POSIX symlinks."""
    if path.is_symlink():
        raise ValueError("deferred update must not be a link")
    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("deferred update is not a regular file")
        if int(file_stat.st_size) > MANAGED_DEFERRED_MAX_RECORD_BYTES:
            raise ValueError(
                "deferred update exceeds the private journal limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MANAGED_DEFERRED_MAX_RECORD_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MANAGED_DEFERRED_MAX_RECORD_BYTES:
        raise ValueError("deferred update exceeds the private journal limit")
    record = json.loads(raw.decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("deferred update is not an object")
    if record.get("version") != MANAGED_DEFERRED_UPDATE_VERSION:
        raise ValueError("unsupported deferred update version")
    update_id = str(record.get("update_id") or "")
    if not update_id:
        raise ValueError("deferred update id is missing")
    if update_id != path.stem:
        raise ValueError("deferred update id does not match its filename")
    if not str(record.get("session_id") or ""):
        raise ValueError("deferred update session id is missing")
    if not str(record.get("job_id") or ""):
        raise ValueError("deferred update job id is missing")
    if record.get("operation") not in {
        "append_log",
        "update_progress",
        "finish",
    }:
        raise ValueError("unsupported deferred update operation")
    if not isinstance(record.get("payload"), dict):
        raise ValueError("deferred update payload is not an object")
    return record


def read_managed_deferred_updates() -> list[
    tuple[Path, dict[str, Any] | None, bool]
]:
    """Return ordered journal rows and whether each invalid row may be removed."""
    rows: list[tuple[Path, dict[str, Any] | None, bool]] = []
    directory = managed_deferred_update_dir(create=False)
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.json")):
        try:
            record = read_managed_deferred_record(path)
        except OSError as exc:
            audit(
                "managed_job_deferred_update_unreadable",
                path=str(path),
                error=repr(exc),
            )
            rows.append((path, None, False))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            audit(
                "managed_job_deferred_update_invalid",
                path=str(path),
                error=repr(exc),
            )
            rows.append((path, None, True))
        else:
            rows.append((path, record, True))
    return rows


def apply_managed_update(
    job: MutableJobRow, operation: str, payload: dict[str, Any]
) -> None:
    """Apply one already validated managed-state update to a mutable job row."""
    match operation:
        case "append_log":
            job["output_bytes"] = int(job.get("output_bytes") or 0) + max(
                0, int(payload.get("bytes") or 0)
            )
            job["log_truncated"] = bool(job.get("log_truncated")) or bool(
                payload.get("truncated")
            )
            job["updated_at"] = float(payload.get("updated_at") or _utc())
            return
        case "update_progress":
            if str(job.get("status") or "") in ACTIVE_STATUSES:
                job["progress"] = _managed_json_dict(
                    payload.get("progress"), label="progress"
                )
                job["updated_at"] = float(payload.get("updated_at") or _utc())
            return
        case "finish":
            if str(job.get("status") or "") not in ACTIVE_STATUSES:
                return
            completed = float(payload.get("completed_at") or _utc())
            job.update(
                {
                    "status": str(payload.get("status") or "failed"),
                    "updated_at": completed,
                    "completed_at": completed,
                    "exit_code": payload.get("exit_code"),
                    "error": payload.get("error"),
                }
            )
            if bool(payload.get("has_result")):
                job["result"] = _managed_json_dict(
                    payload.get("result"), label="result"
                )
            _clear_job_operation(job)
            return
        case _:
            raise ValueError(
                f"unsupported managed update operation: {operation}"
            )


def reconcile_managed_deferred_updates(store: MutableJobStore) -> list[Path]:
    """Replay each journal row once and return rows removable after store commit."""
    rows = read_managed_deferred_updates()
    active_ids = {path.stem for path, _record, _removable in rows}
    for job in store.get("jobs", []):
        if not isinstance(job, dict):
            continue
        applied = [
            str(update_id)
            for update_id in job.get(MANAGED_DEFERRED_APPLIED_KEY, [])
            if str(update_id) in active_ids
        ]
        if applied:
            job[MANAGED_DEFERRED_APPLIED_KEY] = applied
        else:
            job.pop(MANAGED_DEFERRED_APPLIED_KEY, None)

    removable: list[Path] = []
    for path, record, should_remove in rows:
        if should_remove:
            removable.append(path)
        if record is None:
            continue
        session_id = str(record["session_id"])
        job_id = str(record["job_id"])
        try:
            job = _find_session_job(store, session_id, job_id)
        except KeyError:
            continue
        update_id = str(record["update_id"])
        applied = [
            str(item) for item in job.get(MANAGED_DEFERRED_APPLIED_KEY, [])
        ]
        if update_id in applied:
            continue
        try:
            apply_managed_update(
                job,
                str(record["operation"]),
                dict(record["payload"]),
            )
        except (TypeError, ValueError) as exc:
            audit(
                "managed_job_deferred_update_invalid",
                path=str(path),
                session=session_id,
                job_id=job_id,
                error=repr(exc),
            )
            continue
        applied.append(update_id)
        job[MANAGED_DEFERRED_APPLIED_KEY] = applied
    return removable


def remove_managed_deferred_updates(paths: list[Path]) -> None:
    """Remove journal rows only after their applied ids are durably saved."""
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


def job_store_busy_error(*, lock_kind: str) -> TimeoutError:
    """Return one actionable public error while auditing the private lock path."""
    audit(
        "job_store_lock_timeout",
        path=str(_job_store_lock_path()),
        timeout_s=JOB_STORE_LOCK_TIMEOUT_S,
        lock_kind=lock_kind,
    )
    return TimeoutError(
        "job store is busy; another workgate operation or process may be "
        "using the same state directory"
    )


@contextlib.contextmanager
def store_transaction() -> Generator[JobStore]:
    """Serialize one bounded job-store transaction and reconcile deferred updates."""
    started = time.monotonic()
    if not _JOB_STORE_THREAD_LOCK.acquire(timeout=JOB_STORE_LOCK_TIMEOUT_S):
        raise job_store_busy_error(lock_kind="thread")
    try:
        remaining = max(
            0.0,
            JOB_STORE_LOCK_TIMEOUT_S - (time.monotonic() - started),
        )
        lock_stack = contextlib.ExitStack()
        try:
            lock_stack.enter_context(
                private_file_lock(
                    _job_store_lock_path(),
                    timeout_s=remaining,
                    retry_interval_s=JOB_STORE_LOCK_RETRY_INTERVAL_S,
                )
            )
        except TimeoutError as exc:
            raise job_store_busy_error(lock_kind="file") from exc
        with lock_stack:
            store = _load_store()
            deferred_paths = reconcile_managed_deferred_updates(store)
            yield store
            _save_store(store)
            remove_managed_deferred_updates(deferred_paths)
    finally:
        _JOB_STORE_THREAD_LOCK.release()
