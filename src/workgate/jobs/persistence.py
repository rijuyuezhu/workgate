"""Durable tracked-job store and attempt-artifact persistence."""

import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from ..audit import audit
from ..config.settings import get_settings
from ..persistence import get_state_store
from ..utils.private_files import atomic_write_private_text
from .state import JobAttemptPaths, JobRow, JobStore, MutableJobStore

JOB_STORE_FILE_NAME = "jobs.json"
JOB_STORE_BACKUP_FILE_NAME = "jobs.json.bak"
JOB_STORE_LOCK_FILE_NAME = "jobs.lock"
JOB_STORE_VERSION = 2
JOB_STORE_LEGACY_VERSIONS = {1}
TERMINAL_STATUSES = {"succeeded", "failed", "exited", "stopped", "lost"}


def job_store_path() -> Path:
    """Return the primary tracked-job metadata path."""
    return get_state_store().layout.jobs_store_path


def job_store_backup_path() -> Path:
    """Return the fallback tracked-job metadata path."""
    return get_state_store().layout.jobs_store_backup_path


def job_store_lock_path() -> Path:
    """Return the cross-process tracked-job lock path."""
    return get_state_store().layout.jobs_lock_path


def job_runtime_dir() -> Path:
    """Return the private directory containing job attempt logs and status files."""
    path = get_state_store().layout.jobs_dir
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def empty_store() -> JobStore:
    """Return one empty store using the current durable schema version."""
    return {"version": JOB_STORE_VERSION, "jobs": []}


def load_store_file(path: Path) -> JobStore:
    """Load and migrate one supported tracked-job store file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unsupported or invalid job store: {path}")
    version = data.get("version")
    if (
        version != JOB_STORE_VERSION
        and version not in JOB_STORE_LEGACY_VERSIONS
    ):
        raise ValueError(f"unsupported or invalid job store: {path}")
    rows = data.get("jobs")
    if not isinstance(rows, list):
        raise ValueError(f"job store jobs field is invalid: {path}")
    jobs = [cast(JobRow, row) for row in rows if isinstance(row, dict)]
    if version != JOB_STORE_VERSION:
        audit(
            "job_store_migrated",
            path=str(path),
            from_version=version,
            to_version=JOB_STORE_VERSION,
            jobs=len(jobs),
        )
    return {"version": JOB_STORE_VERSION, "jobs": jobs}


def load_store() -> JobStore:
    """Load the primary store or recover from its backup without silent reset."""
    path = job_store_path()
    backup_path = job_store_backup_path()
    if not path.exists() and not backup_path.exists():
        return empty_store()

    main_error: Exception | None = None
    if path.exists():
        try:
            return load_store_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            main_error = exc
            audit("job_store_unreadable", path=str(path), error=repr(exc))

    if backup_path.exists():
        try:
            store = load_store_file(backup_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            audit(
                "job_store_backup_unreadable",
                path=str(backup_path),
                error=repr(exc),
            )
        else:
            audit(
                "job_store_recovered",
                path=str(path),
                backup_path=str(backup_path),
            )
            return store

    raise RuntimeError(
        "Job store is unreadable and no valid backup is available; refusing to reset it"
    ) from main_error


def attempt_paths(job_id: str, attempt: int) -> JobAttemptPaths:
    """Return private command, log, and status paths for one job attempt."""
    stem = f"{job_id}-attempt-{attempt}"
    root = job_runtime_dir()
    return {
        "command": root / f"{stem}.command",
        "log": root / f"{stem}.log",
        "status": root / f"{stem}.status.json",
    }


def remove_attempt_paths(paths: JobAttemptPaths | None) -> None:
    """Remove one explicitly described set of attempt artifacts."""
    if not paths:
        return
    for key in ("command", "log", "status"):
        with contextlib.suppress(OSError):
            paths[key].unlink(missing_ok=True)


def remove_attempt_files(job_id: str, keep_attempt: int | None = None) -> None:
    """Remove runtime artifacts for pruned or superseded attempts."""
    if not job_id:
        return
    keep_stem = (
        f"{job_id}-attempt-{keep_attempt}."
        if keep_attempt is not None
        else None
    )
    for path in job_runtime_dir().glob(f"{job_id}-attempt-*"):
        if keep_stem is not None and path.name.startswith(keep_stem):
            continue
        with contextlib.suppress(OSError):
            path.unlink()


def job_is_retention_terminal(job: Mapping[str, Any]) -> bool:
    """Return whether one row is safe to evict as completed history."""
    status = str(job.get("status") or "")
    return status in TERMINAL_STATUSES and not (
        str(job.get("kind") or "shell") == "shell"
        and status == "lost"
        and not bool(job.get("shell_absence_confirmed"))
    )


def prune_store(store: MutableJobStore, *, max_jobs: int | None = None) -> None:
    """Bound retained terminal jobs while never deleting active operations."""
    jobs = [
        cast(JobRow, row)
        for row in store.get("jobs", [])
        if isinstance(row, dict)
    ]
    limit = max(
        0,
        int(get_settings().max_jobs if max_jobs is None else max_jobs),
    )
    active = [row for row in jobs if not job_is_retention_terminal(row)]
    finished = sorted(
        (row for row in jobs if job_is_retention_terminal(row)),
        key=lambda row: float(
            row.get("updated_at") or row.get("created_at") or 0
        ),
        reverse=True,
    )
    keep_finished = finished[: max(0, limit - len(active))]
    keep_objects = {id(row) for row in [*active, *keep_finished]}
    removed = [row for row in jobs if id(row) not in keep_objects]
    store["jobs"] = [row for row in jobs if id(row) in keep_objects]
    for row in removed:
        remove_attempt_files(str(row.get("job_id") or ""))


def save_store(store: MutableJobStore) -> None:
    """Atomically persist primary and backup stores after retention pruning."""
    prune_store(store)
    store["version"] = JOB_STORE_VERSION
    payload = json.dumps(store, indent=2, sort_keys=True)
    atomic_write_private_text(job_store_path(), payload)
    atomic_write_private_text(job_store_backup_path(), payload)
