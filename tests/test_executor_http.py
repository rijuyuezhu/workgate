from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from workgate.config.settings import Settings
from workgate.control.executor_transport import ExecutorTransport
from workgate.control.http.app import build_http_app
from workgate.control.http.executor_routes import executor_routes
from workgate.control.runtime import build_control_runtime
from workgate.control.state import ControlState, ExecutorTrustRecord
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import (
    ExecutorHelloRequest,
    ExecutorResult,
    ExecutorRuntimeSummary,
)
from workgate.protocol.ids import new_command_id, new_executor_id


def _transport(tmp_path: Path) -> tuple[ExecutorTransport, str, str]:
    state = ControlState(FileStateStore(lambda: tmp_path / "state"))
    state.start()
    executor_id = new_executor_id()
    credential = new_executor_credential()
    state.put_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="executor",
            credential_verifier=executor_credential_verifier(credential),
            created_at=1,
        )
    )
    transport = ExecutorTransport(
        state,
        max_pending_commands=2,
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
    )
    transport.start()
    return transport, executor_id, credential


def _hello_payload() -> dict[str, object]:
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        sessions=(),
        shells=(),
        jobs=(),
    ).model_dump(mode="json")


def test_executor_routes_use_executor_bearer_and_stable_errors(
    tmp_path: Path,
) -> None:
    transport, _, credential = _transport(tmp_path)
    client = TestClient(Starlette(routes=executor_routes(transport)))

    missing = client.post("/executor/v1/hello", json=_hello_payload())
    assert missing.status_code == 401
    assert (
        missing.json()["error"]["code"]
        == ProtocolErrorCode.UNAUTHORIZED_EXECUTOR
    )

    headers = {"Authorization": f"Bearer {credential}"}
    hello = client.post(
        "/executor/v1/hello", json=_hello_payload(), headers=headers
    )
    assert hello.status_code == 200
    assert hello.json() == {
        "protocol_version": 1,
        "heartbeat_interval_s": 10,
        "offline_after_s": 30,
        "poll_timeout_s": 1,
    }
    assert (
        client.post("/executor/v1/heartbeat", headers=headers).status_code
        == 204
    )

    unknown = client.post(
        "/executor/v1/result",
        headers=headers,
        json=ExecutorResult(
            id=new_command_id(), ok=True, result="late"
        ).model_dump(mode="json"),
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == ProtocolErrorCode.UNKNOWN_COMMAND

    malformed = client.post(
        "/executor/v1/result", headers=headers, content=b"not-json"
    )
    assert malformed.status_code == 422
    assert "not-json" not in malformed.text


@pytest.mark.asyncio
async def test_rest_owner_oauth_middleware_bypasses_executor_routes(
    tmp_path: Path,
) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        auth_mode="oauth",
        remote_enabled=False,
    )
    runtime = build_control_runtime(settings)
    runtime.control_state.start()
    runtime.executor_transport.start()
    credential = new_executor_credential()
    runtime.control_state.put_executor(
        ExecutorTrustRecord(
            executor_id=new_executor_id(),
            name="executor",
            credential_verifier=executor_credential_verifier(credential),
            created_at=1,
        )
    )
    app = build_http_app(runtime=runtime)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://control.test",
        ) as client:
            response = await client.post(
                "/executor/v1/heartbeat",
                headers={"Authorization": f"Bearer {credential}"},
            )
        assert response.status_code == 204
    finally:
        await runtime.executor_transport.aclose()
        runtime.control_state.close()
