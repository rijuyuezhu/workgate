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
    ExecutorResult,
    ExecutorRuntimeSummary,
    JobInventorySummary,
    SessionInventorySummary,
    ShellInventorySummary,
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
        assert await second.executor_transport.pending_count(executor_id) == 0
        assert not await second.executor_transport.is_online(executor_id)
        assert await second.executor_transport.inventory(executor_id) is None

        reconnect = ExecutorHelloRequest(
            runtime=ExecutorRuntimeSummary(workgate_version="test"),
            capabilities=("session",),
            sessions=(
                SessionInventorySummary(
                    session_id=session_id,
                    resolved_workdir="/home/user/src/workgate",
                    has_persistent_shells=True,
                    has_active_jobs=True,
                ),
            ),
            shells=(
                ShellInventorySummary(
                    shell_id="shell-after-restart", session_id=session_id
                ),
            ),
            jobs=(
                JobInventorySummary(
                    job_id="job-after-restart",
                    session_id=session_id,
                    status="running",
                ),
            ),
        )
        await second.executor_transport.hello(credential, reconnect)
        assert (
            await second.executor_transport.inventory(executor_id) == reconnect
        )
        assert await second.executor_transport.is_online(executor_id)
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


@pytest.mark.asyncio
async def test_authenticated_hello_does_not_wait_for_post_hello_executor_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_control_runtime(
        Settings(workspace_root=tmp_path, state_dir=tmp_path / "state")
    )
    await runtime.start()
    executor_id = new_executor_id()
    credential = new_executor_credential()
    runtime.control_state.put_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="executor",
            credential_verifier=executor_credential_verifier(credential),
            created_at=1,
        )
    )
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def reconcile_abandonments(*, executor_id: str | None = None) -> None:
        assert executor_id is not None
        cleanup_started.set()
        result = await runtime.executor_transport.call(
            executor_id,
            "transfer_abandon_import",
            {
                "transfer_id": "copy_" + "a" * 22,
                "kind": "file",
                "import_path": "dst.bin",
            },
        )
        assert result.ok is True
        cleanup_finished.set()

    monkeypatch.setattr(
        runtime.session_copy_service,
        "reconcile_abandonments",
        reconcile_abandonments,
    )
    hello = ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        sessions=(),
        shells=(),
        jobs=(),
    )
    try:
        response = await asyncio.wait_for(
            runtime.executor_transport.hello(credential, hello), timeout=0.5
        )
        assert response.poll_timeout_s > 0
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        assert not cleanup_finished.is_set()
        assert await runtime.executor_transport.pending_count(executor_id) == 1

        command = await asyncio.wait_for(
            runtime.executor_transport.poll(credential), timeout=0.5
        )
        assert command is not None
        assert command.op == "transfer_abandon_import"
        await runtime.executor_transport.submit_result(
            credential,
            ExecutorResult(
                id=command.id,
                ok=True,
                result={"safe_to_forget": True},
            ),
        )
        await asyncio.wait_for(cleanup_finished.wait(), timeout=0.5)
    finally:
        await runtime.aclose()
