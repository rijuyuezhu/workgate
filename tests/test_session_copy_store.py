import hashlib
from pathlib import Path

import pytest

from workgate.control.payload_store import PayloadStore
from workgate.control.session_copy_store import SessionCopyCheckpointStore
from workgate.persistence import FileStateStore
from workgate.protocol.ids import new_executor_id, new_session_id


def _stores(tmp_path: Path):
    state = FileStateStore(lambda: tmp_path / "state")
    payloads = PayloadStore(tmp_path / "data")
    return state, payloads, SessionCopyCheckpointStore(state, payloads)


def _commit_checkpoint(
    tmp_path: Path,
    *,
    transfer_id: str = "copy_" + "a" * 22,
    data: bytes = b"payload",
):
    state, payloads, checkpoints = _stores(tmp_path)
    staging = payloads.new_staging_path("transfer")
    with payloads.open_private_staging(staging, namespace="transfer") as handle:
        handle.write(data)
    checkpoint = checkpoints.commit_export(
        checkpoint={
            "transfer_id": transfer_id,
            "owner_job_id": None,
            "source_session_id": str(new_session_id()),
            "source_executor_id": str(new_executor_id()),
            "destination_session_id": str(new_session_id()),
            "destination_executor_id": str(new_executor_id()),
            "source_path": "source.bin",
            "destination_path": "destination.bin",
            "kind": "file",
            "overwrite": True,
            "chunk_size": 1024,
            "source_resolved_path": "source.bin",
            "last_known_step": "exported",
        },
        staging_path=staging,
        payload_size=len(data),
        payload_sha256=hashlib.sha256(data).hexdigest(),
    )
    return state, payloads, checkpoints, checkpoint


def test_corrupt_checkpoint_store_fails_closed_without_payload_cleanup(
    tmp_path,
):
    state, payloads, checkpoints, checkpoint = _commit_checkpoint(tmp_path)
    payload_path = payloads.path(checkpoint.payload_id, namespace="transfer")
    state.write_json(
        checkpoints.path,
        {
            "version": 1,
            "transfers": {
                checkpoint.transfer_id: {"transfer_id": checkpoint.transfer_id}
            },
        },
    )

    with pytest.raises(RuntimeError, match="checkpoint store is invalid"):
        checkpoints.load(checkpoint.transfer_id)
    with pytest.raises(RuntimeError, match="checkpoint store is invalid"):
        checkpoints.prune()

    assert payload_path.read_bytes() == b"payload"


def test_checkpoint_remove_persists_metadata_before_payload_delete(
    tmp_path, monkeypatch
):
    state, payloads, checkpoints, checkpoint = _commit_checkpoint(tmp_path)
    payload_path = payloads.path(checkpoint.payload_id, namespace="transfer")
    real_write_json = state.write_json

    def fail_write(path, value):
        if path == checkpoints.path:
            raise OSError("simulated checkpoint save failure")
        real_write_json(path, value)

    monkeypatch.setattr(state, "write_json", fail_write)
    with pytest.raises(OSError, match="checkpoint save failure"):
        checkpoints.remove(checkpoint.transfer_id)

    assert payload_path.read_bytes() == b"payload"
    monkeypatch.setattr(state, "write_json", real_write_json)
    assert checkpoints.load(checkpoint.transfer_id) is not None


def test_failed_export_checkpoint_write_leaves_collectable_orphan(
    tmp_path, monkeypatch
):
    state, payloads, checkpoints = _stores(tmp_path)
    data = b"orphan-after-ambiguous-state-write"
    staging = payloads.new_staging_path("transfer")
    with payloads.open_private_staging(staging, namespace="transfer") as handle:
        handle.write(data)
    real_write_json = state.write_json

    def fail_write(path, value):
        if path == checkpoints.path:
            raise OSError("simulated export checkpoint failure")
        real_write_json(path, value)

    monkeypatch.setattr(state, "write_json", fail_write)
    with pytest.raises(OSError, match="export checkpoint failure"):
        checkpoints.commit_export(
            checkpoint={
                "transfer_id": "copy_" + "b" * 22,
                "owner_job_id": None,
                "source_session_id": str(new_session_id()),
                "source_executor_id": str(new_executor_id()),
                "destination_session_id": str(new_session_id()),
                "destination_executor_id": str(new_executor_id()),
                "source_path": "source.bin",
                "destination_path": "destination.bin",
                "kind": "file",
                "overwrite": True,
                "chunk_size": 1024,
                "source_resolved_path": "source.bin",
                "last_known_step": "exported",
            },
            staging_path=staging,
            payload_size=len(data),
            payload_sha256=hashlib.sha256(data).hexdigest(),
        )

    orphan_files = list(payloads.directory("transfer").glob("payload_*.bin"))
    assert len(orphan_files) == 1
    monkeypatch.setattr(state, "write_json", real_write_json)
    checkpoints.prune()
    assert list(payloads.directory("transfer").glob("payload_*.bin")) == []


def test_missing_or_malformed_owner_job_never_prunes_managed_checkpoint(
    tmp_path,
):
    owner_job_id = "job_" + "a" * 12
    state, payloads, checkpoints, checkpoint = _commit_checkpoint(tmp_path)
    checkpoint = checkpoints.update(
        checkpoint.transfer_id, owner_job_id=owner_job_id
    )
    payload_path = payloads.path(checkpoint.payload_id, namespace="transfer")

    state.write_json(
        state.layout.jobs_store_path,
        {"version": 2, "jobs": [{"job_id": 17, "status": "succeeded"}]},
    )
    checkpoints.prune()
    assert checkpoints.load(checkpoint.transfer_id) is not None
    assert payload_path.exists()

    state.write_json(state.layout.jobs_store_path, {"version": 2, "jobs": []})
    checkpoints.prune()
    assert checkpoints.load(checkpoint.transfer_id) is not None
    assert payload_path.exists()


def test_confirmed_succeeded_owner_prunes_managed_checkpoint(tmp_path):
    owner_job_id = "job_" + "b" * 12
    state, payloads, checkpoints, checkpoint = _commit_checkpoint(tmp_path)
    checkpoint = checkpoints.update(
        checkpoint.transfer_id, owner_job_id=owner_job_id
    )
    payload_path = payloads.path(checkpoint.payload_id, namespace="transfer")
    state.write_json(
        state.layout.jobs_store_path,
        {
            "version": 2,
            "jobs": [{"job_id": owner_job_id, "status": "succeeded"}],
        },
    )

    checkpoints.prune()

    assert checkpoints.load(checkpoint.transfer_id) is None
    assert not payload_path.exists()
