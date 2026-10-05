import hashlib
import os
import time
from pathlib import Path

import pytest

from workgate.control.payload_store import PayloadStore
from workgate.control.session_copy_store import (
    SessionCopyCheckpointStore,
    TransferPayloadCapacityError,
)
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
    checkpoints.reserve_export(
        transfer_id=transfer_id,
        owner_job_id=None,
        payload_size=len(data),
        max_payload_bytes=max(1, len(data)),
        max_store_bytes=max(1, len(data)),
    )
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


def test_transfer_payload_reservation_enforces_per_transfer_and_store_limits(
    tmp_path,
):
    _state, _payloads, checkpoints = _stores(tmp_path)
    first = "copy_" + "r" * 22
    second = "copy_" + "s" * 22

    with pytest.raises(TransferPayloadCapacityError, match="per-transfer"):
        checkpoints.reserve_export(
            transfer_id=first,
            owner_job_id=None,
            payload_size=11,
            max_payload_bytes=10,
            max_store_bytes=100,
        )

    checkpoints.reserve_export(
        transfer_id=first,
        owner_job_id=None,
        payload_size=7,
        max_payload_bytes=10,
        max_store_bytes=10,
    )
    with pytest.raises(TransferPayloadCapacityError, match="capacity"):
        checkpoints.reserve_export(
            transfer_id=second,
            owner_job_id=None,
            payload_size=4,
            max_payload_bytes=10,
            max_store_bytes=10,
        )


def test_transfer_payload_reservation_survives_store_reconstruction(tmp_path):
    state, payloads, checkpoints = _stores(tmp_path)
    transfer_id = "copy_" + "t" * 22
    checkpoints.reserve_export(
        transfer_id=transfer_id,
        owner_job_id=None,
        payload_size=9,
        max_payload_bytes=10,
        max_store_bytes=10,
    )

    restored = SessionCopyCheckpointStore(state, payloads)
    with pytest.raises(TransferPayloadCapacityError, match="capacity"):
        restored.reserve_export(
            transfer_id="copy_" + "u" * 22,
            owner_job_id=None,
            payload_size=2,
            max_payload_bytes=10,
            max_store_bytes=10,
        )


def test_transfer_payload_commit_converts_reservation_without_double_counting(
    tmp_path,
):
    state, payloads, checkpoints, checkpoint = _commit_checkpoint(
        tmp_path, data=b"1234567"
    )
    restored = SessionCopyCheckpointStore(state, payloads)
    with pytest.raises(TransferPayloadCapacityError, match="capacity"):
        restored.reserve_export(
            transfer_id="copy_" + "v" * 22,
            owner_job_id=None,
            payload_size=1,
            max_payload_bytes=8,
            max_store_bytes=7,
        )

    checkpoints.prepare_abandonment(checkpoint.transfer_id)
    checkpoints.remove(checkpoint.transfer_id)
    restored.reserve_export(
        transfer_id="copy_" + "w" * 22,
        owner_job_id=None,
        payload_size=7,
        max_payload_bytes=7,
        max_store_bytes=7,
    )


def test_prepare_abandonment_releases_uncommitted_reservation(tmp_path):
    _state, _payloads, checkpoints = _stores(tmp_path)
    transfer_id = "copy_" + "x" * 22
    checkpoints.reserve_export(
        transfer_id=transfer_id,
        owner_job_id=None,
        payload_size=5,
        max_payload_bytes=5,
        max_store_bytes=5,
    )

    assert checkpoints.prepare_abandonment(transfer_id) is None

    checkpoints.reserve_export(
        transfer_id="copy_" + "y" * 22,
        owner_job_id=None,
        payload_size=5,
        max_payload_bytes=5,
        max_store_bytes=5,
    )


def test_checkpoint_gc_prunes_only_stale_transfer_staging(
    tmp_path, monkeypatch
):
    _state, payloads, checkpoints = _stores(tmp_path)
    stale = payloads.new_staging_path("transfer")
    fresh = payloads.new_staging_path("transfer")
    for path in (stale, fresh):
        with payloads.open_private_staging(
            path, namespace="transfer"
        ) as handle:
            handle.write(b"partial")
    now = time.time()
    os.utime(stale, (now - 7200, now - 7200))

    checkpoints.prepare_abandonments()

    assert not stale.exists()
    assert fresh.exists()


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
        checkpoints.prepare_abandonments()

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
    transfer_id = "copy_" + "b" * 22
    checkpoints.reserve_export(
        transfer_id=transfer_id,
        owner_job_id=None,
        payload_size=len(data),
        max_payload_bytes=len(data),
        max_store_bytes=len(data),
    )
    real_write_json = state.write_json

    def fail_write(path, value):
        if path == checkpoints.path:
            raise OSError("simulated export checkpoint failure")
        real_write_json(path, value)

    monkeypatch.setattr(state, "write_json", fail_write)
    with pytest.raises(OSError, match="export checkpoint failure"):
        checkpoints.commit_export(
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

    orphan_files = list(payloads.directory("transfer").glob("payload_*.bin"))
    assert len(orphan_files) == 1
    monkeypatch.setattr(state, "write_json", real_write_json)
    checkpoints.prepare_abandonments()
    assert list(payloads.directory("transfer").glob("payload_*.bin")) == []


def test_malformed_job_authority_never_prunes_managed_checkpoint(tmp_path):
    owner_job_id = "job_" + "a" * 12
    state, payloads, checkpoints, checkpoint = _commit_checkpoint(tmp_path)
    checkpoint = checkpoints.update(
        checkpoint.transfer_id, owner_job_id=owner_job_id
    )
    payload_path = payloads.path(checkpoint.payload_id, namespace="transfer")

    assert checkpoints.prepare_abandonments() == ()
    assert checkpoints.load(checkpoint.transfer_id) is not None
    assert payload_path.exists()

    state.write_json(
        state.layout.jobs_store_path,
        {"version": 2, "jobs": [{"job_id": 17, "status": "succeeded"}]},
    )
    assert checkpoints.prepare_abandonments() == ()
    assert checkpoints.load(checkpoint.transfer_id) is not None
    assert payload_path.exists()


def test_authoritative_missing_owner_prunes_unreachable_managed_checkpoint(
    tmp_path,
):
    owner_job_id = "job_" + "c" * 12
    state, payloads, checkpoints, checkpoint = _commit_checkpoint(tmp_path)
    checkpoint = checkpoints.update(
        checkpoint.transfer_id, owner_job_id=owner_job_id
    )
    payload_path = payloads.path(checkpoint.payload_id, namespace="transfer")
    state.write_json(state.layout.jobs_store_path, {"version": 2, "jobs": []})

    candidates = checkpoints.prepare_abandonments()

    assert [item.transfer_id for item in candidates] == [checkpoint.transfer_id]
    retained = checkpoints.load(checkpoint.transfer_id)
    assert retained is not None
    assert retained.abandoning is True
    assert retained.payload_retained is False
    assert not payload_path.exists()


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

    candidates = checkpoints.prepare_abandonments()

    assert [item.transfer_id for item in candidates] == [checkpoint.transfer_id]
    retained = checkpoints.load(checkpoint.transfer_id)
    assert retained is not None
    assert retained.abandoning is True
    assert retained.payload_retained is False
    assert not payload_path.exists()
