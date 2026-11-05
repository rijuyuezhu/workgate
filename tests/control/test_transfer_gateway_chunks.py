"""Request-level Control Relay uploads with strict, recoverable range ACKs."""

import hashlib

from starlette.testclient import TestClient

import workgate.control.transfer_gateway as gateway_module
from tests.control.test_transfer_gateway import _app, _headers, _Transport
from workgate.control.payload_store import PayloadStore
from workgate.control.transfer_gateway import ControlTransferGateway


def _setup(tmp_path, data, digest=None):
    payloads = PayloadStore(tmp_path / "control")
    gateway = ControlTransferGateway(_Transport(), payloads)  # type: ignore[arg-type]
    staging = payloads.new_staging_path("transfer")
    lease = gateway.issue_upload(
        executor_id=_Transport.executor_id,
        transfer_id="copy_" + "b" * 22,
        staging_path=staging,
        expected_bytes=len(data),
        expected_sha256=digest or hashlib.sha256(data).hexdigest(),
    )
    return gateway, staging, lease


def _put(
    client,
    lease,
    payload,
    start,
    total,
    *,
    digest=None,
    bearer="executor-bearer",
):
    return client.put(
        lease.path,
        content=payload,
        headers={
            **_headers(lease.token, bearer=bearer),
            "Content-Range": f"bytes {start}-{start + len(payload) - 1}/{total}",
            "X-Chunk-Sha256": digest or hashlib.sha256(payload).hexdigest(),
        },
    )


def test_chunked_upload_ack_replay_and_verified_completion(tmp_path):
    data = b"hello-" * 37
    gateway, staging, lease = _setup(tmp_path, data)
    with TestClient(_app(gateway)) as client:
        first = _put(client, lease, data[:60], 0, len(data))
        assert first.status_code == 200
        assert first.json() == {
            "offset": 60,
            "bytes": 60,
            "completed": False,
            "sha256": "",
        }
        assert staging.read_bytes() == data[:60]
        retry = _put(client, lease, data[:60], 0, len(data))
        assert retry.status_code == 200
        assert retry.json() == first.json()
        different = _put(client, lease, b"x" * 60, 0, len(data))
        assert different.status_code == 409
        assert different.json()["offset"] == 60
        gap = _put(client, lease, b"bad", 100, len(data))
        assert gap.status_code == 409
        finished = _put(client, lease, data[60:], 60, len(data))
        assert finished.status_code == 200
        assert finished.json() == {
            "offset": len(data),
            "bytes": len(data),
            "completed": True,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        assert (
            _put(client, lease, data[60:], 60, len(data)).json()
            == finished.json()
        )
        assert _put(client, lease, b"x", 0, len(data)).status_code == 409
        assert staging.read_bytes() == data
        gateway.revoke(lease.capability_id)
        assert _put(client, lease, data[60:], 60, len(data)).status_code == 404


def test_chunked_bad_hash_short_body_and_wrong_credential_do_not_consume(
    tmp_path,
):
    data = b"abcdefghij"
    gateway, staging, lease = _setup(tmp_path, data)
    with TestClient(_app(gateway)) as client:
        assert (
            _put(
                client, lease, data[:5], 0, len(data), bearer="wrong"
            ).status_code
            == 404
        )
        assert (
            _put(
                client, lease, data[:5], 0, len(data), digest="0" * 64
            ).status_code
            == 409
        )
        assert staging.exists() and staging.stat().st_size == 0
        malformed = client.put(
            lease.path,
            content=data[:5],
            headers={
                **_headers(lease.token),
                "Content-Range": "bytes nope",
                "X-Chunk-Sha256": hashlib.sha256(data[:5]).hexdigest(),
            },
        )
        assert malformed.status_code == 409
        valid = _put(client, lease, data[:5], 0, len(data))
        assert valid.status_code == 200 and valid.json()["offset"] == 5
        assert _put(client, lease, data[5:], 5, len(data)).status_code == 200
        assert staging.read_bytes() == data


def test_chunked_proxy_limit_and_resume_same_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_module, "MAX_HTTP_TRANSFER_CHUNK_BYTES", 4)
    data = b"abcdefghij"
    gateway, staging, lease = _setup(tmp_path, data)
    with TestClient(_app(gateway)) as client:
        rejected = _put(client, lease, data[:5], 0, len(data))
        assert rejected.status_code == 413
        assert not staging.exists()
        assert _put(client, lease, data[:4], 0, len(data)).json()["offset"] == 4
        assert (
            _put(client, lease, data[4:8], 4, len(data)).json()["offset"] == 8
        )
        assert (
            _put(client, lease, data[8:], 8, len(data)).json()["completed"]
            is True
        )
        assert staging.read_bytes() == data


def test_chunked_final_digest_mismatch_revokes_and_cleans_staging(tmp_path):
    data = b"payload"
    gateway, staging, lease = _setup(
        tmp_path, data, digest=hashlib.sha256(b"wrong").hexdigest()
    )
    with TestClient(_app(gateway)) as client:
        response = _put(client, lease, data, 0, len(data))
        assert response.status_code == 409
        assert response.json()["error"] == "transfer_integrity_mismatch"
        assert not staging.exists()
        assert _put(client, lease, data, 0, len(data)).status_code == 404


def test_replayed_span_requires_the_same_payload(tmp_path):
    data = b"0123456789"
    gateway, staging, lease = _setup(tmp_path, data)
    with TestClient(_app(gateway)) as client:
        assert _put(client, lease, data[:5], 0, len(data)).status_code == 200
        # A replay with the already-confirmed header but different body cannot
        # make the receiver incorrectly confirm its contents.
        changed = client.put(
            lease.path,
            content=b"wrong",
            headers={
                **_headers(lease.token),
                "Content-Range": "bytes 0-4/10",
                "X-Chunk-Sha256": hashlib.sha256(data[:5]).hexdigest(),
            },
        )
        assert changed.status_code == 409
        assert _put(client, lease, data[5:], 5, len(data)).status_code == 200
    assert staging.read_bytes() == data


def test_failed_range_body_and_expired_capability_are_not_accepted(tmp_path):
    current = [10.0]
    payloads = PayloadStore(tmp_path / "control")
    gateway = ControlTransferGateway(
        _Transport(),  # type: ignore[arg-type]
        payloads,
        capability_ttl_s=5,
        clock=lambda: current[0],
    )
    data = b"123456"
    staging = payloads.new_staging_path("transfer")
    lease = gateway.issue_upload(
        executor_id=_Transport.executor_id,
        transfer_id="copy_" + "e" * 22,
        staging_path=staging,
        expected_bytes=len(data),
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )
    with TestClient(_app(gateway)) as client:
        short = client.put(
            lease.path,
            content=data[:2],
            headers={
                **_headers(lease.token),
                "Content-Range": "bytes 0-3/6",
                "X-Chunk-Sha256": hashlib.sha256(data[:4]).hexdigest(),
            },
        )
        assert short.status_code == 409
        assert not staging.exists()
        assert _put(client, lease, data[:3], 0, len(data)).status_code == 200
        current[0] = 16.0
        # An accepted chunk refreshes TTL, but not indefinitely.
        assert _put(client, lease, data[3:], 3, len(data)).status_code == 404
    assert staging.read_bytes() == data[:3]
