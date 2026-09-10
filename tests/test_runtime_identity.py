from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import workgate.utils.runtime_identity as runtime_identity
from workgate.utils.runtime_identity import (
    MANAGED_JOB_LEASE_VERSION,
    ManagedJobLease,
    managed_job_lease_path,
    managed_job_lease_state,
)


def _use_lock_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    locks_dir = tmp_path / "locks"
    store = SimpleNamespace(layout=SimpleNamespace(locks_dir=locks_dir))
    monkeypatch.setattr(
        runtime_identity,
        "get_state_store",
        lambda: cast(Any, store),
    )
    return locks_dir


def test_managed_job_lease_path_is_private_and_opaque(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks_dir = _use_lock_dir(monkeypatch, tmp_path)

    path = managed_job_lease_path("job/with unsafe text")

    assert path.parent == locks_dir
    assert path.name.startswith("managed-job-")
    assert path.name.endswith(".lock")
    assert "unsafe" not in path.name
    assert locks_dir.is_dir()


def test_managed_job_lease_lifecycle_reports_live_then_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_lock_dir(monkeypatch, tmp_path)
    lease = ManagedJobLease("job-live")

    lease.acquire()
    try:
        assert (
            managed_job_lease_state("job-live", MANAGED_JOB_LEASE_VERSION)
            == "live"
        )
    finally:
        lease.release()

    assert (
        managed_job_lease_state("job-live", MANAGED_JOB_LEASE_VERSION) == "dead"
    )


def test_managed_job_lease_state_handles_invalid_and_missing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_lock_dir(monkeypatch, tmp_path)

    assert managed_job_lease_state("", MANAGED_JOB_LEASE_VERSION) == "unknown"
    assert managed_job_lease_state("legacy", object()) == "dead"
    assert (
        managed_job_lease_state("missing", MANAGED_JOB_LEASE_VERSION)
        == "unknown"
    )


def test_managed_job_lease_propagates_lock_thread_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_lock_dir(monkeypatch, tmp_path)

    class BrokenLock:
        def __enter__(self) -> None:
            raise OSError("lock failed")

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        runtime_identity,
        "private_file_lock",
        lambda *_args, **_kwargs: BrokenLock(),
    )
    lease = ManagedJobLease("job-broken")

    with pytest.raises(OSError, match="lock failed"):
        lease.acquire()
    with pytest.raises(OSError, match="lock failed"):
        lease.release()
