import importlib

import pytest

from workgate.control.object_store_transfer import (
    ObjectStoreDependencyError,
    S3ObjectTransferService,
)
from workgate.persistence import FileStateStore


class _FakeS3:
    def __init__(self, *, fail_delete: bool = False) -> None:
        self.fail_delete = fail_delete
        self.deleted: list[tuple[str, str]] = []
        self.presigned: list[tuple[str, str, int]] = []

    def generate_presigned_url(
        self, operation, *, Params, ExpiresIn, HttpMethod
    ):
        self.presigned.append((operation, HttpMethod, ExpiresIn))
        return (
            f"https://storage.test/{Params['Bucket']}/{Params['Key']}"
            f"?operation={operation}&signature=secret"
        )

    def delete_object(self, *, Bucket, Key):
        if self.fail_delete:
            raise OSError("delete failed")
        self.deleted.append((Bucket, Key))


def _service(tmp_path, fake: _FakeS3):
    state = FileStateStore(lambda: tmp_path / "state")
    service = S3ObjectTransferService(
        state,
        bucket="transfer-bucket",
        prefix="/workgate-test/",
        region="test-region",
        endpoint_url="https://s3.test",
        presign_ttl_s=123,
        client_factory=lambda: fake,
    )
    return state, service


@pytest.mark.asyncio
async def test_object_store_attempt_presigns_and_removes_cleanup_record(
    tmp_path,
):
    fake = _FakeS3()
    state, service = _service(tmp_path, fake)
    transfer_id = "copy_" + "a" * 22

    attempt = service.begin_attempt(transfer_id)
    put_url = service.presign_put(attempt)
    get_url = service.presign_get(attempt)
    assert "signature=secret" in put_url
    assert "signature=secret" in get_url
    assert fake.presigned == [
        ("put_object", "PUT", 123),
        ("get_object", "GET", 123),
    ]
    raw = state.read_json(state.layout.control_transfer_objects_path)
    assert isinstance(raw, dict)
    assert raw["objects"][transfer_id]["bucket"] == "transfer-bucket"
    assert raw["objects"][transfer_id]["key"] == (
        f"workgate-test/transfers/{transfer_id}"
    )

    assert await service.finish_attempt(attempt) is None
    assert fake.deleted == [
        ("transfer-bucket", f"workgate-test/transfers/{transfer_id}")
    ]
    raw = state.read_json(state.layout.control_transfer_objects_path)
    assert isinstance(raw, dict)
    assert raw["objects"] == {}


@pytest.mark.asyncio
async def test_object_store_reconciliation_skips_live_attempt(tmp_path):
    fake = _FakeS3()
    _state, service = _service(tmp_path, fake)
    attempt = service.begin_attempt("copy_" + "l" * 22)

    assert await service.reconcile_orphans() == ()
    assert fake.deleted == []

    assert await service.finish_attempt(attempt) is None
    assert len(fake.deleted) == 1


@pytest.mark.asyncio
async def test_object_store_restart_reconciles_persisted_orphan(tmp_path):
    first = _FakeS3()
    state, service = _service(tmp_path, first)
    transfer_id = "copy_" + "b" * 22
    service.begin_attempt(transfer_id)

    second = _FakeS3()
    restored = S3ObjectTransferService(
        state,
        bucket="transfer-bucket",
        prefix="workgate-test",
        region="test-region",
        endpoint_url="https://s3.test",
        presign_ttl_s=123,
        client_factory=lambda: second,
    )
    assert await restored.reconcile_orphans() == ()
    assert second.deleted == [
        ("transfer-bucket", f"workgate-test/transfers/{transfer_id}")
    ]


@pytest.mark.asyncio
async def test_object_store_cleanup_failure_remains_retryable(tmp_path):
    failing = _FakeS3(fail_delete=True)
    state, service = _service(tmp_path, failing)
    transfer_id = "copy_" + "c" * 22
    attempt = service.begin_attempt(transfer_id)

    assert await service.finish_attempt(attempt) == "OSError"
    raw = state.read_json(state.layout.control_transfer_objects_path)
    assert isinstance(raw, dict)
    assert transfer_id in raw["objects"]

    recovered = _FakeS3()
    restored = S3ObjectTransferService(
        state,
        bucket="transfer-bucket",
        prefix="workgate-test",
        region=None,
        endpoint_url=None,
        presign_ttl_s=123,
        client_factory=lambda: recovered,
    )
    assert await restored.reconcile_orphans() == ()
    raw = state.read_json(state.layout.control_transfer_objects_path)
    assert isinstance(raw, dict)
    assert raw["objects"] == {}


def test_object_store_missing_optional_dependency_persists_nothing(
    tmp_path, monkeypatch
):
    state = FileStateStore(lambda: tmp_path / "state")
    service = S3ObjectTransferService(
        state,
        bucket="transfer-bucket",
        prefix="workgate",
        region=None,
        endpoint_url=None,
        presign_ttl_s=123,
    )
    original = importlib.import_module

    def missing(name: str):
        if name == "boto3":
            raise ImportError("missing boto3")
        return original(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(ObjectStoreDependencyError, match=r"workgate\[s3\]"):
        service.begin_attempt("copy_" + "d" * 22)

    assert state.read_json(state.layout.control_transfer_objects_path) is None


@pytest.mark.asyncio
async def test_object_store_pending_cleanup_blocks_new_object(tmp_path):
    failing = _FakeS3(fail_delete=True)
    _state, service = _service(tmp_path, failing)
    first = service.begin_attempt("copy_" + "e" * 22)
    assert await service.finish_attempt(first) == "OSError"

    with pytest.raises(RuntimeError, match="cleanup is pending"):
        service.begin_attempt("copy_" + "f" * 22)


@pytest.mark.asyncio
async def test_object_store_disabled_route_reports_pending_cleanup(tmp_path):
    failing = _FakeS3(fail_delete=True)
    state, service = _service(tmp_path, failing)
    first = service.begin_attempt("copy_" + "g" * 22)
    assert await service.finish_attempt(first) == "OSError"

    disabled = S3ObjectTransferService(
        state,
        bucket=None,
        prefix="workgate-test",
        region=None,
        endpoint_url=None,
        presign_ttl_s=123,
        client_factory=lambda: _FakeS3(),
    )
    assert await disabled.reconcile_orphans() == (
        "object-store cleanup is pending while the route is disabled",
    )


def test_object_store_endpoint_rejects_embedded_credentials(tmp_path):
    state = FileStateStore(lambda: tmp_path / "state")
    with pytest.raises(ValueError, match="without credentials"):
        S3ObjectTransferService(
            state,
            bucket="bucket",
            prefix="workgate",
            region=None,
            endpoint_url="https://user:secret@s3.test",
            presign_ttl_s=123,
        )
