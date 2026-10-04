import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from workgate import __version__
from workgate.config.settings import Settings
from workgate.control import executors as executor_fleet_module
from workgate.control.executor_transport import ExecutorTransportError
from workgate.control.runtime import build_control_runtime
from workgate.control.state import (
    ControlSessionRecord,
    ControlState,
    ExecutorTrustRecord,
)
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import (
    EXECUTOR_CAPABILITY_SESSIONS,
    ExecutorHelloRequest,
    ExecutorResult,
    ExecutorRuntimeSummary,
)
from workgate.protocol.ids import new_executor_id, new_session_id


def _runtime(
    tmp_path: Path,
    *,
    max_agent_sessions: int = 256,
    executor_max_pending_commands: int = 64,
):
    return build_control_runtime(
        Settings(
            auth_mode="none",
            base_url="https://control.test",
            state_dir=tmp_path / "state",
            max_agent_sessions=max_agent_sessions,
            executor_max_pending_commands=executor_max_pending_commands,
        )
    )


def _trust(runtime) -> tuple[str, str]:
    executor_id = new_executor_id()
    credential = new_executor_credential()
    runtime.control_state.put_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="Laptop",
            credential_verifier=executor_credential_verifier(credential),
            created_at=1,
        )
    )
    return executor_id, credential


def _hello() -> ExecutorHelloRequest:
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(
            workgate_version=__version__,
            platform="linux",
        ),
        capabilities=(EXECUTOR_CAPABILITY_SESSIONS,),
        default_workdir="/srv/work",
        sessions=(),
        shells=(),
        jobs=(),
    )


async def _wait_pending(
    runtime, executor_id: str, *, queued: int, offered: int
) -> None:
    for _attempt in range(50):
        status = await runtime.executor_transport.command_status(executor_id)
        if status[:2] == (queued, offered):
            return
        await asyncio.sleep(0)
    raise AssertionError("executor pending state did not converge")


@pytest.mark.asyncio
async def test_fleet_list_explains_eligibility_and_scale_up(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    async with runtime.lifespan():
        executor_id, credential = _trust(runtime)

        offline = await runtime.executor_fleet.list()
        assert offline.session_capacity is not None
        assert offline.session_capacity.available is True
        assert offline.bootstrap is not None
        assert offline.bootstrap.url == (
            "https://control.test/executor/v1/bootstrap"
        )
        assert offline.bootstrap.pairing_approval == "human_required"
        assert offline.executors[0].executor_id == executor_id
        assert offline.executors[0].session_admission is False
        assert offline.executors[0].session_admission_reasons == [
            "offline",
            "inventory_unavailable",
        ]

        await runtime.executor_transport.hello(credential, _hello())
        online = await runtime.executor_fleet.list()
        assert online.bootstrap is None
        entry = online.executors[0]
        assert entry.online is True
        assert entry.capabilities == [EXECUTOR_CAPABILITY_SESSIONS]
        assert entry.runtime is not None
        assert entry.runtime.workgate_version == __version__
        assert entry.runtime_update_required is False
        assert (
            entry.command_limit == runtime.config.executor_max_pending_commands
        )
        assert entry.session_admission is True
        assert entry.session_admission_reasons == []

        routed = await runtime.tool_catalog.handlers()["executor"](
            {"action": "inspect", "executor_id": executor_id}
        )
        assert routed.executor is not None
        assert routed.executor.executor_id == executor_id


@pytest.mark.asyncio
async def test_drain_only_fences_new_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_fleet_module, "audit", lambda *_a, **_k: None)
    runtime = _runtime(tmp_path)
    async with runtime.lifespan():
        executor_id, credential = _trust(runtime)
        await runtime.executor_transport.hello(credential, _hello())

        queued_call = asyncio.create_task(
            runtime.executor_transport.call(
                executor_id, "test.queued", {"value": 1}
            )
        )
        await _wait_pending(runtime, executor_id, queued=1, offered=0)

        drained = await runtime.executor_fleet.execute(
            action="drain", executor_id=executor_id
        )
        assert drained.cancelled_queued is None
        assert drained.preserved_offered is None
        assert drained.executor is not None
        assert drained.executor.draining is True
        assert drained.executor.session_admission is False
        assert "draining" in drained.executor.session_admission_reasons
        await _wait_pending(runtime, executor_id, queued=1, offered=0)

        with pytest.raises(RuntimeError, match="draining"):
            await runtime.session_coordinator.start_session(
                executor_id=executor_id
            )

        resumed = await runtime.executor_fleet.execute(
            action="resume", executor_id=executor_id
        )
        assert resumed.executor is not None
        assert resumed.executor.draining is False
        assert resumed.executor.session_admission is True

        persisted = runtime.control_state.snapshot_executors()[executor_id]
        assert persisted.draining is False

        queued_call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued_call


@pytest.mark.asyncio
async def test_reset_cancels_queued_and_preserves_offered_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_fleet_module, "audit", lambda *_a, **_k: None)
    runtime = _runtime(tmp_path)
    async with runtime.lifespan():
        executor_id, credential = _trust(runtime)
        await runtime.executor_transport.hello(credential, _hello())

        offered_call = asyncio.create_task(
            runtime.executor_transport.call(
                executor_id, "test.offered", {"value": 1}
            )
        )
        await _wait_pending(runtime, executor_id, queued=1, offered=0)
        offered_command = await runtime.executor_transport.poll(credential)
        assert offered_command is not None

        queued_call = asyncio.create_task(
            runtime.executor_transport.call(
                executor_id, "test.queued", {"value": 2}
            )
        )
        await _wait_pending(runtime, executor_id, queued=1, offered=1)

        reset = await runtime.executor_fleet.execute(
            action="reset", executor_id=executor_id
        )
        assert reset.cancelled_queued == 1
        assert reset.preserved_offered == 1

        with pytest.raises(ExecutorTransportError) as reset_error:
            await queued_call
        assert reset_error.value.error.code is ProtocolErrorCode.EXECUTOR_RESET
        assert reset_error.value.delivery_state == "queued"

        await runtime.executor_transport.submit_result(
            credential,
            ExecutorResult(
                id=offered_command.id,
                ok=True,
                result={"finished": True},
            ),
        )
        assert (await offered_call).result == {"finished": True}


@pytest.mark.asyncio
async def test_global_session_limit_does_not_misreport_scale_up(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, max_agent_sessions=1)
    async with runtime.lifespan():
        executor_id, credential = _trust(runtime)
        await runtime.executor_transport.hello(credential, _hello())
        runtime.control_state.put_session(
            ControlSessionRecord(
                session_id=new_session_id(),
                executor_id=executor_id,
                status="active",
                created_at=1,
                updated_at=1,
            )
        )

        fleet = await runtime.executor_fleet.list()

        assert fleet.session_capacity is not None
        assert fleet.session_capacity.available is False
        assert fleet.bootstrap is None
        assert fleet.executors[0].session_admission is False
        assert fleet.executors[0].session_admission_reasons == [
            "session_capacity_full"
        ]


@pytest.mark.asyncio
async def test_command_capacity_blocks_session_admission_and_suggests_scale_up(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, executor_max_pending_commands=1)
    async with runtime.lifespan():
        executor_id, credential = _trust(runtime)
        await runtime.executor_transport.hello(credential, _hello())

        pending_call = asyncio.create_task(
            runtime.executor_transport.call(
                executor_id, "test.pending", {"value": 1}
            )
        )
        await _wait_pending(runtime, executor_id, queued=1, offered=0)

        fleet = await runtime.executor_fleet.list()

        assert fleet.bootstrap is not None
        assert fleet.executors[0].session_admission is False
        assert fleet.executors[0].session_admission_reasons == [
            "command_capacity_full"
        ]

        pending_call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_call


@pytest.mark.asyncio
async def test_drain_state_survives_control_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_fleet_module, "audit", lambda *_a, **_k: None)
    runtime = _runtime(tmp_path)
    executor_id = ""
    async with runtime.lifespan():
        executor_id, credential = _trust(runtime)
        await runtime.executor_transport.hello(credential, _hello())
        await runtime.executor_fleet.execute(
            action="drain", executor_id=executor_id
        )
        assert runtime.control_state.snapshot_executors()[executor_id].draining

    reloaded = ControlState(runtime.state_store)
    reloaded.start()
    try:
        assert reloaded.snapshot_executors()[executor_id].draining is True
    finally:
        reloaded.close()


@pytest.mark.asyncio
async def test_fleet_mutations_are_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        executor_fleet_module,
        "audit",
        lambda event, **fields: events.append((event, fields)),
    )
    runtime = _runtime(tmp_path)
    async with runtime.lifespan():
        executor_id, credential = _trust(runtime)
        await runtime.executor_transport.hello(credential, _hello())

        await runtime.executor_fleet.execute(
            action="rename", executor_id=executor_id, name="Desk"
        )
        await runtime.executor_fleet.execute(
            action="reset", executor_id=executor_id
        )
        await runtime.executor_fleet.execute(
            action="drain", executor_id=executor_id
        )
        await runtime.executor_fleet.execute(
            action="resume", executor_id=executor_id
        )
        await runtime.executor_fleet.execute(
            action="revoke", executor_id=executor_id
        )

    assert [event for event, _fields in events] == [
        "executor_renamed",
        "executor_reset",
        "executor_drain_changed",
        "executor_drain_changed",
        "executor_revoked",
    ]


@pytest.mark.asyncio
async def test_executor_tool_requires_executor_use_scope(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    async with runtime.lifespan():
        handler = runtime.tool_catalog.handlers()["executor"]

        token = bind_oauth_claims({"scope": "shell:read"})
        try:
            with pytest.raises(HTTPException) as exc_info:
                await handler({"action": "list"})
        finally:
            reset_oauth_claims(token)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == (
            "Missing required OAuth scope: executor:use"
        )

        token = bind_oauth_claims({"scope": "executor:use"})
        try:
            result = await handler({"action": "list"})
        finally:
            reset_oauth_claims(token)
        assert result.action == "list"
