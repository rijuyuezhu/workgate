import asyncio
from pathlib import Path

import pytest

from workgate.config.settings import Settings
from workgate.control.executor_transport import ExecutorTransportClosedError
from workgate.control.runtime import build_control_runtime
from workgate.control.state import ControlSessionRecord, ExecutorTrustRecord
from workgate.oauth.core.client_store import persist_approved_clients
from workgate.oauth.core.models import AuthCode, OAuthClient
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.executor import (
    ExecutorHelloRequest,
    ExecutorRuntimeSummary,
)
from workgate.protocol.ids import new_executor_id, new_session_id


@pytest.mark.asyncio
async def test_control_runtime_restores_only_durable_product_facts(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=state_dir,
        remote_enabled=False,
    )
    first = build_control_runtime(settings)

    assert first.control_state.state_store is first.services.state_store
    assert first.oauth_state.state_store is first.services.state_store

    await first.start()
    executor_id = new_executor_id()
    session_id = new_session_id()
    credential = new_executor_credential()
    trust = ExecutorTrustRecord(
        executor_id=executor_id,
        name="laptop",
        credential_verifier=executor_credential_verifier(credential),
        created_at=10,
    )
    session = ControlSessionRecord(
        session_id=session_id,
        executor_id=executor_id,
        requested_workdir="~/src/workgate",
        resolved_workdir_display="/home/user/src/workgate",
        label="workgate",
        status="active",
        created_at=20,
        updated_at=30,
    )
    approved_client = OAuthClient(
        client_id="approved",
        redirect_uris=["https://client.example/callback"],
        client_name="Approved client",
        created_at=40,
        approved_at=50,
    )

    first.control_state.put_executor(trust)
    first.control_state.put_session(session)
    first.oauth_state.clients[approved_client.client_id] = approved_client
    persist_approved_clients(
        first.oauth_state.clients,
        state_store=first.oauth_state.state_store,
    )
    first.oauth_state.codes["transient"] = AuthCode(
        code="transient",
        client_id=approved_client.client_id,
        redirect_uri="https://client.example/callback",
        scope="shell:read",
        resource="https://workgate.example/mcp",
        code_challenge="challenge",
        code_challenge_method="S256",
    )
    marker = first.human_ui_runtime.terminal_connections.reserve(4)
    assert marker is not None
    await first.executor_transport.hello(
        credential,
        ExecutorHelloRequest(
            runtime=ExecutorRuntimeSummary(workgate_version="test"),
            sessions=(),
            shells=(),
            jobs=(),
        ),
    )
    pending_call = asyncio.create_task(
        first.executor_transport.call(executor_id, "shell.run")
    )
    await asyncio.sleep(0)
    assert await first.executor_transport.pending_count(executor_id) == 1

    await first.aclose()
    with pytest.raises(ExecutorTransportClosedError):
        await pending_call

    assert first.control_state.snapshot_executors() == {}
    assert first.control_state.snapshot_sessions() == {}
    assert first.oauth_state.clients == {}
    assert first.oauth_state.codes == {}

    second = build_control_runtime(settings)
    await second.start()
    try:
        assert second.control_state.snapshot_executors() == {executor_id: trust}
        assert second.control_state.snapshot_sessions() == {session_id: session}
        assert second.oauth_state.clients == {
            approved_client.client_id: approved_client
        }
        assert second.oauth_state.codes == {}
        assert second.human_ui_runtime.terminal_connections.active_count() == 0
        assert second.human_ui_runtime.remote_files.snapshot() == ()
        assert await second.executor_transport.pending_count(executor_id) == 0
        assert not await second.executor_transport.is_online(executor_id)
        assert await second.executor_transport.inventory(executor_id) is None
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_control_runtime_start_failure_discards_control_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=state_dir,
        remote_enabled=False,
    )
    seed = build_control_runtime(settings)
    seed.control_state.start()
    record = ExecutorTrustRecord(
        executor_id=new_executor_id(),
        name="executor",
        credential_verifier=executor_credential_verifier(
            new_executor_credential()
        ),
        created_at=10,
    )
    seed.control_state.put_executor(record)
    seed.control_state.close()

    runtime = build_control_runtime(settings)

    async def fail_start() -> None:
        assert runtime.control_state.snapshot_executors() == {
            record.executor_id: record
        }
        raise RuntimeError("managed jobs start failed")

    monkeypatch.setattr(runtime.managed_jobs_runtime, "start", fail_start)

    with pytest.raises(RuntimeError, match="managed jobs start failed"):
        await runtime.start()

    assert runtime.control_state.snapshot_executors() == {}
    with pytest.raises(RuntimeError, match="not running"):
        runtime.control_state.put_executor(record)
