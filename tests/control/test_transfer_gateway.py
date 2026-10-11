import hashlib

from starlette.applications import Starlette
from starlette.testclient import TestClient

from workgate.control.executor_transport import ExecutorTransportError
from workgate.control.payload_store import PayloadStore
from workgate.control.transfer_gateway import ControlTransferGateway
from workgate.http.request_limits import RequestBodyLimitMiddleware
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import (
    EXECUTOR_TRANSFER_PREFIX,
    EXECUTOR_TRANSFER_TOKEN_HEADER,
)


class _Transport:
    executor_id = "exec_test"

    def authenticate_live_bearer(self, bearer: str) -> str:
        if bearer != "executor-bearer":
            raise ExecutorTransportError(
                ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
                "unauthorized",
            )
        return self.executor_id


def _app(gateway: ControlTransferGateway) -> Starlette:
    app = Starlette(routes=gateway.routes())
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=8,
        streaming_prefixes=(EXECUTOR_TRANSFER_PREFIX,),
    )
    return app


def _headers(token: str, *, bearer: str = "executor-bearer") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        EXECUTOR_TRANSFER_TOKEN_HEADER: token,
    }


def test_raw_upload_streams_past_shared_body_limit_and_is_single_claim(
    tmp_path,
):
    payloads = PayloadStore(tmp_path / "data")
    gateway = ControlTransferGateway(_Transport(), payloads)  # type: ignore[arg-type]
    data = b"raw-transfer-payload" * 32
    staging = payloads.new_staging_path("transfer")
    lease = gateway.issue_upload(
        executor_id=_Transport.executor_id,
        transfer_id="copy_" + "a" * 22,
        staging_path=staging,
        expected_bytes=len(data),
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )

    with TestClient(_app(gateway)) as client:
        wrong_direction = client.get(
            lease.path,
            headers=_headers(lease.token),
        )
        assert wrong_direction.status_code == 404

        wrong_bearer = client.put(
            lease.path,
            content=data,
            headers=_headers(lease.token, bearer="wrong"),
        )
        assert wrong_bearer.status_code == 404

        wrong_token = client.put(
            lease.path,
            content=data,
            headers=_headers("wrong-token"),
        )
        assert wrong_token.status_code == 404

        response = client.put(
            lease.path,
            content=data,
            headers=_headers(lease.token),
        )
        assert response.status_code == 200
        assert response.json() == {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        assert staging.read_bytes() == data

        replay = client.put(
            lease.path,
            content=data,
            headers=_headers(lease.token),
        )
        assert replay.status_code == 404


def test_raw_upload_integrity_failure_deletes_staging(tmp_path):
    payloads = PayloadStore(tmp_path / "data")
    gateway = ControlTransferGateway(_Transport(), payloads)  # type: ignore[arg-type]
    data = b"actual-data"
    staging = payloads.new_staging_path("transfer")
    lease = gateway.issue_upload(
        executor_id=_Transport.executor_id,
        transfer_id="copy_" + "b" * 22,
        staging_path=staging,
        expected_bytes=len(data),
        expected_sha256=hashlib.sha256(b"different").hexdigest(),
    )

    with TestClient(_app(gateway)) as client:
        response = client.put(
            lease.path,
            content=data,
            headers=_headers(lease.token),
        )

    assert response.status_code == 409
    assert not staging.exists()


def test_raw_download_starts_at_capability_bound_offset(tmp_path):
    payloads = PayloadStore(tmp_path / "data")
    gateway = ControlTransferGateway(_Transport(), payloads)  # type: ignore[arg-type]
    data = b"0123456789"
    staging = payloads.new_staging_path("transfer")
    with payloads.open_private_staging(staging, namespace="transfer") as handle:
        handle.write(data)
    payload = payloads.commit_staging(
        staging,
        namespace="transfer",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    lease = gateway.issue_download(
        executor_id=_Transport.executor_id,
        transfer_id="copy_" + "c" * 22,
        payload_id=str(payload.payload_id),
        expected_bytes=len(data),
        expected_sha256=hashlib.sha256(data).hexdigest(),
        offset=4,
    )

    with TestClient(_app(gateway)) as client:
        response = client.get(lease.path, headers=_headers(lease.token))
        assert response.status_code == 200
        assert response.content == data[4:]
        assert response.headers["content-length"] == str(len(data) - 4)
        assert response.headers["x-workgate-transfer-offset"] == "4"
        assert response.headers["cache-control"] == "no-store"

        replay = client.get(lease.path, headers=_headers(lease.token))
        assert replay.status_code == 404


def test_expired_capability_fails_closed_without_consuming_other_records(
    tmp_path,
):
    now = [10.0]
    payloads = PayloadStore(tmp_path / "data")
    gateway = ControlTransferGateway(
        _Transport(),  # type: ignore[arg-type]
        payloads,
        capability_ttl_s=5,
        clock=lambda: now[0],
    )
    data = b"x"
    staging = payloads.new_staging_path("transfer")
    lease = gateway.issue_upload(
        executor_id=_Transport.executor_id,
        transfer_id="copy_" + "d" * 22,
        staging_path=staging,
        expected_bytes=1,
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )
    now[0] = 16.0

    with TestClient(_app(gateway)) as client:
        response = client.put(
            lease.path,
            content=data,
            headers=_headers(lease.token),
        )

    assert response.status_code == 404
    assert not staging.exists()
