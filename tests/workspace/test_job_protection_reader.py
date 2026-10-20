"""Durable Job protection must prevent unsafe executor Session cleanup."""

from pathlib import Path

from workgate.executor.tool_session.retention import JobProtectionReader
from workgate.persistence import FileStateStore


def _reader(tmp_path: Path) -> tuple[FileStateStore, JobProtectionReader]:
    state = FileStateStore(lambda: tmp_path)
    return state, JobProtectionReader(state, status_max_bytes=4096)


def test_active_job_protects_session_until_durable_completion(
    tmp_path: Path,
) -> None:
    state, reader = _reader(tmp_path)
    session_id = "sess_0000000000000000000001"
    status = tmp_path / "jobs" / "job_1.status.json"
    job = {
        "job_id": "job_1",
        "kind": "shell",
        "status": "running",
        "session_id": session_id,
        "status_path": str(status),
    }
    assert reader.active_session_ids() == set()

    state.write_json(state.layout.jobs_store_path, {"jobs": [job]})
    assert reader.active_session_ids() == {session_id}

    # A runner receipt is stronger evidence than stale "running" metadata.
    state.write_json(status, {"exit_code": 0})
    assert reader.active_session_ids() == set()


def test_job_protection_uses_backup_and_fails_closed_on_invalid_stores(
    tmp_path: Path,
) -> None:
    state, reader = _reader(tmp_path)
    session_id = "sess_0000000000000000000002"
    state.write_json(
        state.layout.jobs_store_backup_path,
        {
            "jobs": [
                {"kind": "shell", "status": "running", "session_id": session_id}
            ]
        },
    )
    state.write_json(state.layout.jobs_store_path, {"not_jobs": []})
    assert reader.active_session_ids() == {session_id}

    # Without a valid primary or backup, assume jobs can still be active.
    state.write_json(state.layout.jobs_store_backup_path, {"bad": "state"})
    assert reader.active_session_ids() is None
