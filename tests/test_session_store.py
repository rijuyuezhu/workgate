import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

from workgate.config.settings import clear_settings_cache
from workgate.tool_session import retention as retention_module
from workgate.tool_session import store as store_module
from workgate.tool_session.store import (
    SESSION_TERMINATION_PROMPT,
    ExpiredAgentSessionError,
    SessionTerminationRequestedError,
    UnknownAgentSessionError,
    get_tool_session_store,
)
from workgate.utils import runtime_identity as runtime_identity_module
from workgate.utils.runtime_identity import (
    MANAGED_JOB_LEASE_VERSION,
    ManagedJobLease,
    managed_job_lease_state,
)


def _start_peer_managed_job_lease(
    job_id: str, marker, release
) -> subprocess.Popen[str]:
    script = f"""
import time
from pathlib import Path
from workgate.utils.runtime_identity import ManagedJobLease

lease = ManagedJobLease({job_id!r})
lease.acquire()
Path({str(marker)!r}).write_text("live", encoding="utf-8")
while not Path({str(release)!r}).exists():
    time.sleep(0.01)
lease.release()
"""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_peer_marker(
    process: subprocess.Popen[str], marker, *, timeout_s: float = 5.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"peer lease process exited early\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        time.sleep(0.01)
    raise AssertionError("peer lease process did not acquire its lock")


def test_managed_job_lease_reports_unknown_live_and_dead(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    job_id = "job_lease_state"

    assert (
        managed_job_lease_state(job_id, MANAGED_JOB_LEASE_VERSION) == "unknown"
    )
    assert managed_job_lease_state(job_id, 0) == "dead"

    lease = ManagedJobLease(job_id)
    lease.acquire()
    try:
        assert (
            managed_job_lease_state(job_id, MANAGED_JOB_LEASE_VERSION) == "live"
        )
    finally:
        lease.release()

    assert managed_job_lease_state(job_id, MANAGED_JOB_LEASE_VERSION) == "dead"


def test_managed_job_lease_propagates_lock_acquisition_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()

    @contextmanager
    def failing_lock(*_args, **_kwargs):
        raise OSError("lease lock failed")
        yield

    monkeypatch.setattr(
        runtime_identity_module, "private_file_lock", failing_lock
    )

    with pytest.raises(OSError, match="lease lock failed"):
        ManagedJobLease("job_lock_error").acquire()


def test_managed_job_lease_state_is_unknown_on_lock_io_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    job_id = "job_state_io_error"
    path = runtime_identity_module.managed_job_lease_path(job_id)
    path.touch(mode=0o600)

    @contextmanager
    def failing_lock(*_args, **_kwargs):
        raise OSError("lease state failed")
        yield

    monkeypatch.setattr(
        runtime_identity_module, "private_file_lock", failing_lock
    )

    assert (
        managed_job_lease_state(job_id, MANAGED_JOB_LEASE_VERSION) == "unknown"
    )


def test_create_session_returns_8_character_alnum_id(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()

    session = store.create_session(workdir=".")

    assert re.fullmatch(r"[A-Za-z0-9]{8}", session.session_id)
    assert session.target == "local"
    assert session.workdir == str(tmp_path)
    assert session.machine is None
    assert session.worker_session_id is None


def test_create_session_retries_id_collisions(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    ids = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(store_module, "_generate_session_id", lambda: next(ids))

    first = store.create_session(workdir=".")
    second = store.create_session(workdir=".")

    assert first.session_id == "aaaaaaaa"
    assert second.session_id == "bbbbbbbb"


def test_require_session_rejects_unknown_session_id():
    store = get_tool_session_store()
    store.clear()

    with pytest.raises(UnknownAgentSessionError):
        store.require_session("missing1")


def test_snapshots_are_isolated_by_explicit_session(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    first = store.create_session(workdir=".")
    second = store.create_session(workdir=".")

    record = store.record_file_snapshot(
        session_id=first.session_id,
        path="a.txt",
        file_sha256="abc",
        total_lines=1,
        seen_ranges=((1, 1),),
    )

    assert store.get_snapshot(first.session_id, record.snapshot_id) == record
    assert store.get_snapshot(second.session_id, record.snapshot_id) is None


def test_change_session_workdir_updates_session_and_clears_snapshots(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    store = get_tool_session_store()
    store.clear()
    session = store.create_session(workdir="first")
    record = store.record_file_snapshot(
        session_id=session.session_id,
        path="a.txt",
        file_sha256="abc",
        total_lines=1,
        seen_ranges=((1, 1),),
    )

    updated = store.change_session_workdir(session.session_id, "second")

    assert updated.session_id == session.session_id
    assert updated.workdir == str(second_dir)
    assert updated.updated_at >= session.updated_at
    assert store.get_snapshot(session.session_id, record.snapshot_id) is None


def test_change_session_workdir_crash_after_snapshot_invalidation_keeps_old_cwd(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    store = store_module.ToolSessionStore()
    session = store.create_session(workdir="first")
    snapshot = store.record_file_snapshot(
        session_id=session.session_id,
        path="a.txt",
        file_sha256="abc",
        total_lines=1,
        seen_ranges=((1, 1),),
    )
    remove_snapshots = store._snapshot_repository.remove_session

    def crash_after_snapshot_invalidation(session_id: str) -> None:
        remove_snapshots(session_id)
        raise RuntimeError("simulated crash after snapshot invalidation")

    monkeypatch.setattr(
        store._snapshot_repository,
        "remove_session",
        crash_after_snapshot_invalidation,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        store.change_session_workdir(session.session_id, "second")

    restarted = store_module.ToolSessionStore()
    surviving = restarted.require_session(session.session_id)
    assert surviving.workdir == str(first_dir)
    assert (
        restarted.get_snapshot(session.session_id, snapshot.snapshot_id) is None
    )


def test_change_session_workdir_crash_after_cwd_persist_keeps_new_cwd(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    store = store_module.ToolSessionStore()
    session = store.create_session(workdir="first")
    snapshot = store.record_file_snapshot(
        session_id=session.session_id,
        path="a.txt",
        file_sha256="abc",
        total_lines=1,
        seen_ranges=((1, 1),),
    )
    write_session = store._write_session_locked

    def crash_after_cwd_persist(updated) -> None:
        write_session(updated)
        raise RuntimeError("simulated crash after cwd persistence")

    monkeypatch.setattr(store, "_write_session_locked", crash_after_cwd_persist)

    with pytest.raises(RuntimeError, match="simulated crash"):
        store.change_session_workdir(session.session_id, "second")

    restarted = store_module.ToolSessionStore()
    surviving = restarted.require_session(session.session_id)
    assert surviving.workdir == str(second_dir)
    assert (
        restarted.get_snapshot(session.session_id, snapshot.snapshot_id) is None
    )


def test_update_remote_session_workdir_clears_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    remote = store.create_session(
        target="remote",
        workdir="/remote/first",
        machine="worker-a",
        worker_session_id="WORKER12",
    )
    snapshot = store.record_file_snapshot(
        session_id=remote.session_id,
        path="file.txt",
        file_sha256="a" * 64,
        total_lines=1,
        seen_ranges=((1, 1),),
    )

    updated = store.update_remote_session_workdir(
        remote.session_id, "/remote/second"
    )

    assert updated.workdir == "/remote/second"
    assert store.get_snapshot(remote.session_id, snapshot.snapshot_id) is None
    with pytest.raises(ValueError, match="must not be empty"):
        store.update_remote_session_workdir(remote.session_id, "")

    local = store.create_session(target="local", workdir=tmp_path)
    with pytest.raises(ValueError, match="not remote"):
        store.update_remote_session_workdir(local.session_id, "/remote")


def test_remote_session_creation_requires_complete_durable_binding(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()

    with pytest.raises(ValueError, match="worker_session_id is required"):
        store.create_session(
            target="remote", workdir="/remote/work", machine="worker-a"
        )
    with pytest.raises(ValueError, match="workdir is required"):
        store.create_session(
            target="remote",
            workdir="",
            machine="worker-a",
            worker_session_id="WORKER12",
        )

    assert store.list_sessions() == []


def test_remote_session_round_trips_through_cold_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    first_store = store_module.ToolSessionStore()
    first_store.clear()
    session = first_store.create_session(
        target="remote",
        workdir="/remote/work",
        machine="worker-a",
        worker_session_id="WORKER12",
    )

    restored = store_module.ToolSessionStore().require_session(
        session.session_id
    )

    assert restored == session
    assert restored.machine == "worker-a"
    assert restored.worker_session_id == "WORKER12"


def test_sessions_and_snapshots_survive_new_store_instance(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    first_store = store_module.ToolSessionStore()
    first_store.clear()
    session = first_store.create_session(workdir=tmp_path, label="durable")
    snapshot = first_store.record_file_snapshot(
        session_id=session.session_id,
        path="file.txt",
        file_sha256="b" * 64,
        total_lines=2,
        seen_ranges=((1, 2),),
    )

    second_store = store_module.ToolSessionStore()

    assert second_store.require_session(session.session_id) == session
    assert (
        second_store.get_snapshot(session.session_id, snapshot.snapshot_id)
        == snapshot
    )
    assert second_store.list_sessions() == [session]
    session_dir = tmp_path / ".state" / "sessions" / session.session_id
    assert (session_dir / "session.json").is_file()
    assert (session_dir / "snapshots.json").is_file()


def test_termination_request_is_durable_idempotent_and_blocks_tool_work(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path)

    terminated = store.request_termination(session.session_id)
    repeated = store.request_termination(session.session_id)

    assert terminated.termination_requested_at is not None
    assert (
        repeated.termination_requested_at == terminated.termination_requested_at
    )
    restored = store_module.ToolSessionStore().require_session(
        session.session_id
    )
    assert (
        restored.termination_requested_at == terminated.termination_requested_at
    )
    with pytest.raises(SessionTerminationRequestedError) as exc_info:
        store.assert_tool_call_allowed((session.session_id,))
    assert exc_info.value.session_id == session.session_id
    assert SESSION_TERMINATION_PROMPT in str(exc_info.value)


def test_tool_input_session_ids_only_reads_semantic_argument_envelopes():
    assert store_module.tool_input_session_ids(
        {
            "session_id": "TOP00001",
            "src_session_id": "SRC00001",
            "dst_session_id": "DST00001",
            "env": {"session_id": "IGNORED1"},
            "payload": [{"session_id": "IGNORED2"}],
            "kwargs": {
                "source_session_id": "SOURCE01",
                "env": {"session_id": "IGNORED3"},
            },
            "keyword_args": {
                "destination_session_id": "DEST0001",
            },
        }
    ) == ("TOP00001", "SRC00001", "DST00001", "SOURCE01", "DEST0001")


def test_explicit_session_expiry_is_enforced_and_removes_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)

    with pytest.raises(ExpiredAgentSessionError, match="expired session_id"):
        store.require_session(session.session_id)

    session_dir = tmp_path / ".state" / "sessions" / session.session_id
    assert not session_dir.exists()


def test_idle_retention_prunes_sessions_when_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "1")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path)
    clock[0] = 102.0

    assert store.list_sessions() == []
    session_dir = tmp_path / ".state" / "sessions" / session.session_id
    assert not session_dir.exists()


def test_active_session_capacity_is_rejected_without_eviction(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "1")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    store.create_session(workdir=tmp_path)

    with pytest.raises(RuntimeError, match="agent session limit reached"):
        store.create_session(workdir=tmp_path)


def test_inactive_session_is_evicted_to_make_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "1")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    first = store.create_session(workdir=tmp_path)
    clock[0] += store_module.SESSION_ACTIVE_WINDOW_S + 1

    second = store.create_session(workdir=tmp_path)

    with pytest.raises(UnknownAgentSessionError):
        store.require_session(first.session_id)
    assert store.require_session(second.session_id) == second


def test_snapshot_count_retention_keeps_newest_records(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_SESSION_SNAPSHOTS", "2")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path)
    records = []
    for index in range(3):
        clock[0] += 1
        records.append(
            store.record_file_snapshot(
                session_id=session.session_id,
                path=f"{index}.txt",
                file_sha256=str(index),
                total_lines=1,
                seen_ranges=((1, 1),),
            )
        )

    assert (
        store.get_snapshot(session.session_id, records[0].snapshot_id) is None
    )
    assert (
        store.get_snapshot(session.session_id, records[1].snapshot_id)
        is not None
    )
    assert (
        store.get_snapshot(session.session_id, records[2].snapshot_id)
        is not None
    )


def test_snapshot_count_retention_favors_new_insertion_when_timestamps_tie(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_SESSION_SNAPSHOTS", "1")
    clear_settings_cache()
    monkeypatch.setattr(store_module.time, "time", lambda: 100.0)
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path)
    first = store.record_file_snapshot(
        session_id=session.session_id,
        path="first.txt",
        file_sha256="first",
        total_lines=1,
        seen_ranges=((1, 1),),
    )
    second = store.record_file_snapshot(
        session_id=session.session_id,
        path="second.txt",
        file_sha256="second",
        total_lines=1,
        seen_ranges=((1, 1),),
    )

    reloaded = store_module.ToolSessionStore()
    assert reloaded.get_snapshot(session.session_id, first.snapshot_id) is None
    assert (
        reloaded.get_snapshot(session.session_id, second.snapshot_id) == second
    )
    assert second.sequence > first.sequence


def test_snapshot_sequence_migrates_legacy_rows_and_favors_new_record(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_SESSION_SNAPSHOTS", "1")
    clear_settings_cache()
    monkeypatch.setattr(store_module.time, "time", lambda: 100.0)
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path)
    legacy = {
        "session_id": session.session_id,
        "snapshot_id": "legacy-snapshot",
        "path": "legacy.txt",
        "file_sha256": "legacy",
        "total_lines": 1,
        "seen_ranges": [[1, 1]],
        "created_at": 100.0,
    }
    store._state_store.write_json(
        store._state_store.layout.session_snapshots_path(session.session_id),
        {"snapshots": [legacy]},
    )

    current = store.record_file_snapshot(
        session_id=session.session_id,
        path="current.txt",
        file_sha256="current",
        total_lines=1,
        seen_ranges=((1, 1),),
    )

    assert store.get_snapshot(session.session_id, "legacy-snapshot") is None
    assert (
        store.get_snapshot(session.session_id, current.snapshot_id) == current
    )
    assert current.sequence == 2


def test_snapshot_metadata_rejects_one_record_over_byte_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_SESSION_SNAPSHOT_BYTES", "1024")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path)

    with pytest.raises(ValueError, match="max_session_snapshot_bytes"):
        store.record_file_snapshot(
            session_id=session.session_id,
            path="large.txt",
            file_sha256="a" * 64,
            total_lines=2_000,
            seen_ranges=tuple((line, line) for line in range(1, 2_001)),
        )


def test_expired_session_with_active_job_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": "job_active",
                    "session_id": session.session_id,
                    "status": "running",
                }
            ],
        },
    )

    assert store.require_session(session.session_id) == session


def test_expired_session_with_unconfirmed_lost_shell_job_is_preserved(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": "job_lost",
                    "kind": "shell",
                    "session_id": session.session_id,
                    "shell_id": "shell_unknown",
                    "status": "lost",
                }
            ],
        },
    )

    assert store.require_session(session.session_id) == session


def test_confirmed_absent_lost_shell_job_does_not_block_expiry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": "job_lost",
                    "kind": "shell",
                    "session_id": session.session_id,
                    "shell_id": "shell_missing",
                    "status": "lost",
                    "shell_absence_confirmed": True,
                }
            ],
        },
    )

    with pytest.raises(ExpiredAgentSessionError):
        store.require_session(session.session_id)


@pytest.mark.parametrize("primary_state", ["missing", "corrupt"])
def test_active_job_backup_protects_expired_session(
    tmp_path, monkeypatch, primary_state
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    payload = {
        "version": 1,
        "jobs": [
            {
                "job_id": "job_active",
                "session_id": session.session_id,
                "status": "running",
            }
        ],
    }
    store._state_store.write_json(
        store._state_store.layout.jobs_store_backup_path, payload
    )
    if primary_state == "corrupt":
        store._state_store.layout.jobs_store_path.write_text(
            "{not-json", encoding="utf-8"
        )

    assert store.require_session(session.session_id) == session


def test_invalid_primary_and_backup_conservatively_protect_sessions(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    store._state_store.layout.jobs_store_path.write_text(
        "{not-json", encoding="utf-8"
    )
    store._state_store.layout.jobs_store_backup_path.write_text(
        "[]", encoding="utf-8"
    )

    assert store.require_session(session.session_id) == session


def test_expired_session_with_persistent_shell_is_preserved(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=101.0)
    registered = store.register_persistent_shell(
        session.session_id, "shell-one"
    )
    clock[0] = 102.0

    assert registered.persistent_shell_ids == ("shell-one",)
    assert store.require_session(session.session_id) == registered


def test_persistent_shell_registry_releases_and_reconciles(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    first = store.create_session(workdir=tmp_path)
    second = store.create_session(workdir=tmp_path)
    store.register_persistent_shell(first.session_id, "shell-one")
    store.register_persistent_shell(first.session_id, "shell-two")
    store.register_persistent_shell(second.session_id, "shell-three")

    store.release_persistent_shell("shell-one")
    assert store.require_session(first.session_id).persistent_shell_ids == (
        "shell-two",
    )

    store.reconcile_persistent_shells({"shell-three"})
    assert store.require_session(first.session_id).persistent_shell_ids == ()
    assert store.require_session(second.session_id).persistent_shell_ids == (
        "shell-three",
    )


def test_scoped_shell_reconciliation_does_not_clear_other_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    first = store.create_session(workdir=tmp_path)
    second = store.create_session(workdir=tmp_path)
    store.register_persistent_shell(first.session_id, "reused")
    store.register_persistent_shell(second.session_id, "reused")

    reconciled = store.reconcile_session_persistent_shells(
        first.session_id, set()
    )

    assert reconciled.persistent_shell_ids == ()
    assert store.require_session(second.session_id).persistent_shell_ids == (
        "reused",
    )


def test_scoped_shell_reservation_rollback_preserves_other_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    original = store.create_session(workdir=tmp_path)
    contender = store.create_session(workdir=tmp_path)
    store.register_persistent_shell(original.session_id, "reused")

    assert (
        store.reserve_persistent_shell(contender.session_id, "reused") is True
    )
    assert (
        store.reserve_persistent_shell(contender.session_id, "reused") is False
    )

    rolled_back = store.release_session_persistent_shell(
        contender.session_id, "reused"
    )

    assert rolled_back.persistent_shell_ids == ()
    assert store.require_session(original.session_id).persistent_shell_ids == (
        "reused",
    )


def test_exclusive_shell_reservation_rejects_other_session_owner(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    first = store.create_session(workdir=tmp_path)
    second = store.create_session(workdir=tmp_path)

    assert store.reserve_persistent_shell(
        first.session_id, "shared-name", exclusive=True
    )
    with pytest.raises(
        RuntimeError, match="already reserved by another session"
    ):
        store.reserve_persistent_shell(
            second.session_id, "shared-name", exclusive=True
        )

    assert store.require_session(first.session_id).persistent_shell_ids == (
        "shared-name",
    )
    assert store.require_session(second.session_id).persistent_shell_ids == ()


def test_persistent_shell_ids_unions_all_durable_sessions(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    first = store.create_session(workdir=tmp_path)
    second = store.create_session(workdir=tmp_path)
    store.register_persistent_shell(first.session_id, "shell-one")
    store.register_persistent_shell(second.session_id, "shell-two")

    assert store.persistent_shell_ids() == {"shell-one", "shell-two"}


def test_inactive_session_with_persistent_shell_is_not_evicted(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "1")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path)
    store.register_persistent_shell(session.session_id, "shell-one")
    clock[0] += store_module.SESSION_ACTIVE_WINDOW_S + 1

    with pytest.raises(RuntimeError, match="agent session limit reached"):
        store.create_session(workdir=tmp_path)


def test_reserved_shell_blocks_expiry_until_authoritative_reconciliation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=101.0)
    store.register_persistent_shell(session.session_id, "reserved-shell")
    clock[0] = 102.0

    assert store.require_session(session.session_id).persistent_shell_ids == (
        "reserved-shell",
    )

    store.reconcile_persistent_shells(set())

    with pytest.raises(ExpiredAgentSessionError):
        store.require_session(session.session_id)


def test_stale_managed_job_from_prior_runtime_does_not_block_expiry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": "job_stale_managed",
                    "session_id": session.session_id,
                    "kind": "managed",
                    "status": "running",
                    "runtime_instance_id": "previous-process",
                }
            ],
        },
    )

    with pytest.raises(ExpiredAgentSessionError):
        store.require_session(session.session_id)


def test_legacy_managed_job_does_not_protect_payload_endpoint(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    source = store.create_session(workdir=tmp_path)
    destination = store.create_session(
        workdir=tmp_path, expires_at=time.time() - 1
    )
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": "job_legacy_copy",
                    "session_id": source.session_id,
                    "kind": "managed",
                    "managed_kind": "session-copy",
                    "status": "running",
                    "runtime_instance_id": "previous-process",
                    "managed_payload": {
                        "src_session_id": source.session_id,
                        "dst_session_id": destination.session_id,
                    },
                }
            ],
        },
    )

    with pytest.raises(ExpiredAgentSessionError):
        store.require_session(destination.session_id)


def test_live_peer_managed_job_still_protects_expired_session(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        retention_module, "managed_job_lease_state", lambda *_args: "live"
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": "job_current_managed",
                    "session_id": session.session_id,
                    "kind": "managed",
                    "status": "running",
                    "runtime_instance_id": "peer-process",
                    "managed_lease_version": 1,
                }
            ],
        },
    )

    assert store.require_session(session.session_id) == session


def test_real_peer_managed_job_lease_protects_payload_endpoints_until_exit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    source = store.create_session(workdir=tmp_path)
    destination = store.create_session(
        workdir=tmp_path, expires_at=time.time() - 1
    )
    job_id = "job_peer_retention"
    marker = tmp_path / "peer-retention-live"
    release = tmp_path / "release-peer-retention"
    process = _start_peer_managed_job_lease(job_id, marker, release)
    try:
        _wait_for_peer_marker(process, marker)
        store._state_store.write_json(
            store._state_store.layout.jobs_store_path,
            {
                "version": 2,
                "jobs": [
                    {
                        "job_id": job_id,
                        "session_id": source.session_id,
                        "kind": "managed",
                        "status": "running",
                        "runtime_instance_id": "peer-runtime",
                        "managed_lease_version": 1,
                        "managed_payload": {
                            "src_session_id": source.session_id,
                            "dst_session_id": destination.session_id,
                        },
                    }
                ],
            },
        )

        assert store.require_session(destination.session_id) == destination

        release.write_text("release", encoding="utf-8")
        assert process.wait(timeout=5) == 0
        with pytest.raises(ExpiredAgentSessionError):
            store.require_session(destination.session_id)
    finally:
        release.touch(exist_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_current_managed_job_protects_destination_session_from_expiry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        retention_module, "managed_job_lease_state", lambda *_args: "live"
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    source = store.create_session(workdir=tmp_path)
    destination = store.create_session(
        workdir=tmp_path, expires_at=time.time() - 1
    )
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": "job_copy",
                    "session_id": source.session_id,
                    "kind": "managed",
                    "status": "running",
                    "runtime_instance_id": "peer-process",
                    "managed_lease_version": 1,
                    "managed_payload": {
                        "src_session_id": source.session_id,
                        "dst_session_id": destination.session_id,
                        "nested": [{"ignored_session_id": "not-valid"}],
                    },
                }
            ],
        },
    )

    assert store.require_session(destination.session_id) == destination


def test_current_managed_job_destination_blocks_capacity_eviction(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        retention_module, "managed_job_lease_state", lambda *_args: "live"
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "2")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    source = store.create_session(workdir=tmp_path)
    destination = store.create_session(workdir=tmp_path)
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": "job_copy",
                    "session_id": source.session_id,
                    "kind": "managed",
                    "status": "running",
                    "runtime_instance_id": "peer-process",
                    "managed_lease_version": 1,
                    "managed_payload": {
                        "dst_session_id": destination.session_id
                    },
                }
            ],
        },
    )
    clock[0] += store_module.SESSION_ACTIVE_WINDOW_S + 1

    with pytest.raises(RuntimeError, match="agent session limit reached"):
        store.create_session(workdir=tmp_path)

    assert {item.session_id for item in store.list_sessions()} == {
        source.session_id,
        destination.session_id,
    }


@pytest.mark.parametrize(
    ("status", "path_key"),
    [("running", "status_path"), ("retrying", "pending_status_path")],
)
def test_durable_job_completion_no_longer_blocks_session_expiry(
    tmp_path, monkeypatch, status, path_key
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    status_path = tmp_path / ".state" / "jobs" / f"job_{status}.status.json"
    store._state_store.write_json(
        status_path,
        {"exit_code": 0, "completed_at": time.time()},
    )
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {
            "version": 1,
            "jobs": [
                {
                    "job_id": f"job_{status}",
                    "session_id": session.session_id,
                    "status": status,
                    path_key: str(status_path),
                }
            ],
        },
    )

    with pytest.raises(ExpiredAgentSessionError):
        store.require_session(session.session_id)


def test_expired_remote_session_binding_is_preserved_for_explicit_cleanup(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(
        target="remote",
        workdir="/remote/work",
        machine="worker-a",
        worker_session_id="WORKER12",
        expires_at=time.time() - 1,
    )

    with pytest.raises(ExpiredAgentSessionError, match="call session_end"):
        store.require_session(session.session_id)

    session_dir = tmp_path / ".state" / "sessions" / session.session_id
    assert session_dir.exists()
    assert store.list_sessions() == [session]

    prepared = store.prepare_session_termination(session.session_id)
    assert prepared.worker_session_id == "WORKER12"
    assert prepared.expires_at is None
    assert prepared.termination_requested_at is not None
    assert store.end_session(session.session_id) == prepared
    assert not session_dir.exists()


def test_inactive_remote_session_is_not_evicted_without_worker_cleanup(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "1")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    remote = store.create_session(
        target="remote",
        workdir="/remote/work",
        machine="worker-a",
        worker_session_id="WORKER12",
    )
    clock[0] += store_module.SESSION_ACTIVE_WINDOW_S + 1

    with pytest.raises(RuntimeError, match="agent session limit reached"):
        store.create_session(workdir=tmp_path)

    assert store.list_sessions() == [remote]


def test_expiry_prune_revalidates_concurrent_touch(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "10")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path)
    peer = store_module.ToolSessionStore()
    clock[0] = 200.0
    candidate_path = store._transaction_path(session.session_id)
    original_transaction = store._state_store.transaction
    injected = False

    @contextmanager
    def injecting_transaction(path):
        nonlocal injected
        if path == candidate_path and not injected:
            injected = True
            with original_transaction(candidate_path):
                current = peer._load_session_locked(session.session_id)
                assert current is not None
                updated = store_module.replace(current, updated_at=clock[0])
                peer._write_session_locked(updated)
        with original_transaction(path):
            yield

    monkeypatch.setattr(
        store._state_store, "transaction", injecting_transaction
    )

    assert store.list_sessions() == [store.require_session(session.session_id)]


def test_expiry_prune_revalidates_concurrent_job_admission(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    clear_settings_cache()
    store = store_module.ToolSessionStore()
    store.clear()
    session = store.create_session(workdir=tmp_path, expires_at=time.time() - 1)
    candidate_path = store._transaction_path(session.session_id)
    jobs_lock_path = store._state_store.layout.jobs_lock_path
    original_transaction = store._state_store.transaction
    injected = False

    @contextmanager
    def injecting_transaction(path):
        nonlocal injected
        if path == candidate_path and not injected:
            injected = True
            with original_transaction(jobs_lock_path):
                store._state_store.write_json(
                    store._state_store.layout.jobs_store_path,
                    {
                        "version": 1,
                        "jobs": [
                            {
                                "job_id": "job_active",
                                "kind": "shell",
                                "session_id": session.session_id,
                                "status": "running",
                            }
                        ],
                    },
                )
        with original_transaction(path):
            yield

    monkeypatch.setattr(
        store._state_store, "transaction", injecting_transaction
    )

    assert store.list_sessions() == [session]


def test_overflow_prune_revalidates_concurrent_touch(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "2")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    oldest = store.create_session(workdir=tmp_path)
    clock[0] += 1
    newer = store.create_session(workdir=tmp_path)
    peer = store_module.ToolSessionStore()
    clock[0] += store_module.SESSION_ACTIVE_WINDOW_S + 1
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "1")
    clear_settings_cache()
    candidate_path = store._transaction_path(oldest.session_id)
    original_transaction = store._state_store.transaction
    injected = False

    @contextmanager
    def injecting_transaction(path):
        nonlocal injected
        if path == candidate_path and not injected:
            injected = True
            with original_transaction(candidate_path):
                current = peer._load_session_locked(oldest.session_id)
                assert current is not None
                updated = store_module.replace(current, updated_at=clock[0])
                peer._write_session_locked(updated)
        with original_transaction(path):
            yield

    monkeypatch.setattr(
        store._state_store, "transaction", injecting_transaction
    )

    assert [item.session_id for item in store.list_sessions()] == [
        oldest.session_id
    ]
    with pytest.raises(UnknownAgentSessionError):
        store.require_session(newer.session_id)


def test_pruning_loads_active_job_owners_once(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_SESSION_RETENTION_S", "0")
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "3")
    clear_settings_cache()
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    store = store_module.ToolSessionStore()
    store.clear()
    for _ in range(3):
        store.create_session(workdir=tmp_path)
        clock[0] += 1

    clock[0] += store_module.SESSION_ACTIVE_WINDOW_S + 1
    monkeypatch.setenv("WORKGATE_MAX_AGENT_SESSIONS", "1")
    clear_settings_cache()
    store._state_store.write_json(
        store._state_store.layout.jobs_store_path,
        {"version": 1, "jobs": []},
    )
    original_read_json = store._state_store.read_json
    job_store_reads = 0

    def counted_read_json(path, *, max_bytes=None):
        nonlocal job_store_reads
        if path == store._state_store.layout.jobs_store_path:
            job_store_reads += 1
        return original_read_json(path, max_bytes=max_bytes)

    monkeypatch.setattr(store._state_store, "read_json", counted_read_json)

    assert len(store.list_sessions()) == 1
    assert job_store_reads == 3
