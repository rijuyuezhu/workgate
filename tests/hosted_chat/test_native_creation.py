"""Paired native Task/Session creation with durable intent reconciliation."""

import time

import pytest

from tests.support.paired import build_paired_control_harness
from workgate.audit import query_audit
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.execution_context import control_execution_context
from workgate.control.executor_transport import ExecutorTransportError
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.durable_tasks import DurableTaskRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.native_creation import (
    NativeCreationUncertain,
    create_native,
)
from workgate.hosted_chat.provider_common import ModelToolCall
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import SESSION_CREATE_OP, SESSION_LOOKUP_OP

SCOPES = {"shell:read", "shell:write", "shell:execute"}


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()
    (tmp_path / "project").mkdir()
    harness = build_paired_control_harness(get_settings())
    database = SQLiteDatabase(harness.control.config.state_dir)
    generation, _ = GenerationStore(database).begin(
        "local-user", {"text": "native work"}
    )
    lease = ActorLease.claim(database, "local-user", generation.run_id)
    assert lease is not None
    yield harness, DurableTaskRepository(database), lease, generation
    clear_settings_cache()


async def invoke(harness, repo, lease, call, *, scopes=SCOPES):
    return await create_native(
        harness.control,
        repo,
        lease,
        call,
        machine=harness.executor_id,
        scopes=scopes,
        expires=None,
    )


@pytest.mark.asyncio
async def test_native_creation_recovers_lost_response_without_reoffering(
    bound, monkeypatch
):
    harness, repo, lease, generation = bound
    runtime = harness.control
    proposal = ModelToolCall(
        "task-call", "task", {"action": "create", "label": "native task"}
    )
    first = await invoke(harness, repo, lease, proposal)
    task_id = first["task_id"]
    assert (await invoke(harness, repo, lease, proposal))["task_id"] == task_id
    assert len(await runtime.task_service.list_tasks()) == 1

    transport = runtime.executor_transport
    original = transport.call
    offered = []
    lookup_allowed = False

    async def lost_response(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == SESSION_LOOKUP_OP and not lookup_allowed:
            raise TimeoutError("offline")
        response = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == SESSION_CREATE_OP:
            offered.append(session_id)
            raise ExecutorTransportError(
                ProtocolErrorCode.EXECUTOR_OFFLINE,
                "lost response",
                delivery_state="offered",
            )
        return response

    monkeypatch.setattr(transport, "call", lost_response)
    session = ModelToolCall(
        "session-call",
        "session_start",
        {
            "executor_id": harness.executor_id,
            "task_id": task_id,
            "workdir": "project",
        },
    )
    with pytest.raises(ExecutorTransportError, match="lost response"):
        await invoke(harness, repo, lease, session)
    with pytest.raises(NativeCreationUncertain):
        await invoke(harness, repo, lease, session)
    assert len(offered) == 1
    assert not repo.owned(
        "local-user",
        generation.conversation_id,
        "session",
        offered[0],
        machine=harness.executor_id,
    )
    lookup_allowed = True
    recovered = await invoke(harness, repo, lease, session)
    assert recovered["recovered"] and recovered["session_id"] == offered[0]
    assert (
        recovered["executor_id"] == harness.executor_id
        and recovered["task_id"] == task_id
    )
    assert "environment" not in recovered
    assert (
        len(offered) == 1
        and len(runtime.control_state.snapshot_sessions()) == 1
    )
    assert repo.owned(
        "local-user",
        generation.conversation_id,
        "session",
        recovered["session_id"],
        machine=harness.executor_id,
    )
    with control_execution_context(
        config=runtime.config, state_store=runtime.state_store
    ):
        entries = query_audit(event="tool_call_end", limit=100)["entries"]
    assert any(
        row.get("tool") == "session_start"
        and row.get("session") == recovered["session_id"]
        and row.get("paired") is True
        and row.get("status") == "success"
        for row in entries
    )


@pytest.mark.asyncio
async def test_native_grant_and_principal_cannot_be_widened(bound):
    harness, repo, lease, _ = bound
    proposal = ModelToolCall("task-call", "task", {"action": "create"})
    token = bind_oauth_claims(
        {
            "sub": "other",
            "scope": "shell:read shell:write",
            "exp": int(time.time()) + 120,
        }
    )
    try:
        with pytest.raises(PermissionError, match="principal"):
            await invoke(harness, repo, lease, proposal)
    finally:
        reset_oauth_claims(token)
    token = bind_oauth_claims(
        {
            "sub": "local-user",
            "scope": "shell:read",
            "exp": int(time.time()) + 120,
        }
    )
    try:
        with pytest.raises(PermissionError, match="shell:write"):
            await invoke(harness, repo, lease, proposal)
    finally:
        reset_oauth_claims(token)
    with repo.database.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM agent_durable_tasks").fetchone()[0]
            == 0
        )
    assert await harness.control.task_service.list_tasks() == []


@pytest.mark.asyncio
async def test_cancel_racing_creation_retains_native_receipt_without_disclosure(
    bound, monkeypatch
):
    harness, repo, lease, generation = bound
    service = harness.control.task_service
    create = service.create_task

    async def create_then_cancel(**kwargs):
        result = await create(**kwargs)
        repo.cancel_run(lease.owner, lease.run_id)
        return result

    monkeypatch.setattr(service, "create_task", create_then_cancel)
    proposal = ModelToolCall("task-call", "task", {"action": "create"})
    with pytest.raises(PermissionError, match="cancelled"):
        await invoke(harness, repo, lease, proposal)
    tasks = await service.list_tasks()
    assert len(tasks) == 1
    with repo.database.connect() as db:
        row = db.execute(
            "SELECT native_id,state FROM agent_durable_tasks WHERE run_id=?",
            (generation.run_id,),
        ).fetchone()
        assert (
            row["state"] == "cancel_pending"
            and row["native_id"] == tasks[0].task_id
        )
    with pytest.raises(PermissionError):
        await invoke(harness, repo, lease, proposal)
    assert len(await service.list_tasks()) == 1


@pytest.mark.asyncio
async def test_native_creation_confirmed_normal_session_and_replay(bound):
    harness, repo, lease, _ = bound
    task = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall("task", "task", {"action": "create"}),
    )
    call = ModelToolCall(
        "session",
        "session_start",
        {
            "executor_id": harness.executor_id,
            "task_id": task["task_id"],
            "workdir": "project",
        },
    )
    first = await invoke(harness, repo, lease, call)
    again = await invoke(harness, repo, lease, call)
    assert first["session_id"] == again["session_id"]
    assert again["recovered"] is True
    assert len(harness.control.control_state.snapshot_sessions()) == 1


@pytest.mark.asyncio
async def test_native_creation_missing_task_receipt_never_reoffers(
    bound, monkeypatch
):
    harness, repo, lease, _ = bound
    attempts = []

    async def unavailable(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("receipt unavailable")

    monkeypatch.setattr(
        harness.control.task_service, "create_task", unavailable
    )
    call = ModelToolCall("task", "task", {"action": "create"})
    with pytest.raises(TimeoutError):
        await invoke(harness, repo, lease, call)
    with pytest.raises(NativeCreationUncertain):
        await invoke(harness, repo, lease, call)
    assert len(attempts) == 1
    assert not await harness.control.task_service.list_tasks()
