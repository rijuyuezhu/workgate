import hashlib
import os
from pathlib import Path

import pytest

from workgate.control.payload_store import PayloadStore


def _store(tmp_path: Path) -> PayloadStore:
    return PayloadStore(tmp_path / "data")


def _commit(
    tmp_path: Path,
    data: bytes,
    *,
    namespace: str = "test",
):
    store = _store(tmp_path)
    staging = store.new_staging_path(namespace)
    with store.open_private_staging(staging, namespace=namespace) as handle:
        handle.write(data)
    return store, store.commit_staging(
        staging,
        namespace=namespace,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def test_payload_commit_and_verified_open(tmp_path):
    data = b"immutable payload"
    store, payload = _commit(tmp_path, data)

    assert payload.payload_id.startswith("payload_")
    assert payload.size == len(data)
    assert payload.sha256 == hashlib.sha256(data).hexdigest()
    assert store.path(
        payload.payload_id, namespace="test"
    ).parent == store.directory("test")

    handle, path = store.open_payload(
        payload.payload_id,
        namespace="test",
        size=payload.size,
        sha256=payload.sha256,
    )
    try:
        assert handle.read() == data
        assert path == store.path(payload.payload_id, namespace="test")
    finally:
        handle.close()

    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert store.directory("test").stat().st_mode & 0o777 == 0o700


def test_payload_open_rejects_size_and_digest_changes(tmp_path):
    store, payload = _commit(tmp_path, b"original")
    path = store.path(payload.payload_id, namespace="test")

    path.write_bytes(b"different-size")
    with pytest.raises(ValueError, match="size changed"):
        store.open_payload(
            payload.payload_id,
            namespace="test",
            size=payload.size,
            sha256=payload.sha256,
        )

    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest changed"):
        store.open_payload(
            payload.payload_id,
            namespace="test",
            size=len(b"tampered"),
            sha256=payload.sha256,
        )


def test_payload_staging_cleanup_is_feature_namespaced(tmp_path):
    store = _store(tmp_path)
    download = store.new_staging_path("download")
    transfer = store.new_staging_path("transfer")
    download.write_bytes(b"download")
    transfer.write_bytes(b"transfer")
    old = 1_700_000_000
    os.utime(download, (old, old))
    os.utime(transfer, (old, old))

    assert store.prune_staging_files("download") is True
    assert not download.exists()
    assert transfer.read_bytes() == b"transfer"


def test_committed_payload_cleanup_is_feature_namespaced(tmp_path):
    store, download = _commit(tmp_path, b"download", namespace="download")
    _, transfer = _commit(tmp_path, b"transfer", namespace="transfer")
    download_path = store.path(download.payload_id, namespace="download")
    transfer_path = store.path(transfer.payload_id, namespace="transfer")

    assert store.prune_unreferenced_payloads("download", set()) is True
    assert not download_path.exists()
    assert transfer_path.read_bytes() == b"transfer"


def test_referenced_committed_payload_survives_cleanup(tmp_path):
    store, payload = _commit(tmp_path, b"payload", namespace="download")
    path = store.path(payload.payload_id, namespace="download")

    assert (
        store.prune_unreferenced_payloads("download", {payload.payload_id})
        is False
    )
    assert path.read_bytes() == b"payload"


def test_payload_ids_and_namespaces_are_validated(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="invalid payload id"):
        store.path("../escape", namespace="test")
    with pytest.raises(ValueError, match="invalid payload namespace"):
        store.new_staging_path("../transfer")


def test_remove_payload_is_idempotent(tmp_path):
    store, payload = _commit(tmp_path, b"payload")
    path = store.path(payload.payload_id, namespace="test")

    store.remove_payload(payload.payload_id, namespace="test")
    store.remove_payload(payload.payload_id, namespace="test")

    assert not path.exists()
