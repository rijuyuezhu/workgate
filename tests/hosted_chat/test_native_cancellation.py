"""Reconcile only ledger-owned native resources, including offline recovery."""

import time

import pytest

from tests.support.paired import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.executor_transport import ExecutorTransportError
from workgate.control.hosted_chat_cancellation import sweep_native_cancellations
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.durable_tasks import DurableTaskRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.provider_common import ModelToolCall
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import SESSION_TERMINATE_OP

SCOPES = {"shell:read", "shell:write", "shell:execute"}


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()
    harness = build_paired_control_harness(get_settings())
    database = SQLiteDatabase(harness.control.config.state_dir)
    yield harness, DurableTaskRepository(database)
    clear_settings_cache()


def generation(repo):
    result, _ = GenerationStore(repo.database).begin(
        "local-user", {"text": "native work"}
    )
    lease = ActorLease.claim(repo.database, "local-user", result.run_id)
    assert lease is not None
    return result, lease


def reserve(harness, repo, lease, call, *, dispatch=True, expires=None):
    args = {
        "scopes": SCOPES,
        "expires": expires,
        "registered": (harness.executor_id,),
    }
    intent = repo.reserve(lease, call, machine=harness.executor_id, **args)
    if dispatch:
        assert repo.claim(lease, intent, call, **args)
    return intent


async def create(harness, repo, lease, *, confirm_session=True):
    task = reserve(
        harness,
        repo,
        lease,
        ModelToolCall("task", "task", {"action": "create"}),
    )
    await harness.control.task_service.create_task(reserved_task_id=task.key)
    repo.bind(lease, task, task.key)
    session = reserve(
        harness,
        repo,
        lease,
        ModelToolCall(
            "session",
            "session_start",
            {"task_id": task.key, "executor_id": harness.executor_id},
        ),
    )
    await harness.control.session_coordinator.start_session(
        task_id=task.key,
        executor_id=harness.executor_id,
        reserved_session_id=session.key,
    )
    if confirm_session:
        repo.bind(lease, session, session.key)
    return task, session


def rows(repo):
    with repo.database.connect() as db:
        return {
            row["idempotency_key"]: dict(row)
            for row in db.execute("SELECT * FROM agent_durable_tasks")
        }


def due(repo):
    with repo.database.connect() as db:
        db.execute("UPDATE agent_durable_tasks SET next_attempt_at=NULL")


@pytest.mark.asyncio
async def test_offline_abort_recovers_unreturned_session_and_preserves_other_work(
    bound, monkeypatch
):
    harness, repo = bound
    run, lease = generation(repo)
    task, session = await create(harness, repo, lease, confirm_session=False)
    other_run, other_lease = generation(repo)
    other_task, other_session = await create(harness, repo, other_lease)
    transport = harness.control.executor_transport
    original = transport.call
    offline = True
    attempted = []

    async def call(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == SESSION_TERMINATE_OP:
            attempted.append((executor_id, session_id))
            if offline:
                raise ExecutorTransportError(
                    ProtocolErrorCode.EXECUTOR_OFFLINE, "offline"
                )
        return await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )

    monkeypatch.setattr(transport, "call", call)
    repo.cancel_run(lease.owner, run.run_id)
    assert await sweep_native_cancellations(harness.control, repo) == 0
    assert rows(repo)[session.key]["state"] == "cancel_pending"
    assert rows(repo)[task.key]["state"] == "cancel_pending"
    assert rows(repo)[other_session.key]["state"] == "active"
    assert not repo.is_run_cancelled(other_run.run_id)
    offline = False
    due(repo)
    assert await sweep_native_cancellations(harness.control, repo) >= 1
    due(repo)
    await sweep_native_cancellations(harness.control, repo)
    current = rows(repo)
    assert current[session.key]["state"] == "stopped"
    assert current[session.key]["native_id"] == session.key
    assert current[task.key]["state"] == "stopped"
    assert (
        await harness.control.task_service.read_task(task.key)
    ).status == "cancelled"
    assert harness.executor.sessions.lookup(other_session.key) is not None
    assert (
        await harness.control.task_service.read_task(other_task.key)
    ).status == "active"
    assert set(attempted) == {(harness.executor_id, session.key)}


@pytest.mark.asyncio
async def test_reserved_abort_and_unknown_absence_never_create(bound):
    harness, repo = bound
    run, lease = generation(repo)
    reserved = reserve(
        harness,
        repo,
        lease,
        ModelToolCall("reserved", "task", {"action": "create"}),
        dispatch=False,
    )
    unknown = reserve(
        harness,
        repo,
        lease,
        ModelToolCall("unknown", "task", {"action": "create"}),
    )
    repo.cancel_run(lease.owner, run.run_id)
    assert rows(repo)[reserved.key]["state"] == "stopped"
    assert await sweep_native_cancellations(harness.control, repo) == 0
    assert rows(repo)[unknown.key]["state"] == "cancel_pending"
    assert not await harness.control.task_service.list_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["complete", "interrupted", "error"])
async def test_normal_generation_terminal_state_preserves_native_work(
    bound, status
):
    harness, repo = bound
    run, lease = generation(repo)
    task, session = await create(harness, repo, lease)
    with repo.database.connect() as db:
        db.execute("UPDATE runs SET status=? WHERE id=?", (status, run.run_id))
    assert await sweep_native_cancellations(harness.control, repo) == 0
    assert (
        rows(repo)[task.key]["state"]
        == rows(repo)[session.key]["state"]
        == "active"
    )
    assert harness.executor.sessions.lookup(session.key) is not None
    # Policy loss after completion must still cancel the retained native work.
    WorkspaceRepository(repo.database).save_setting(
        "local-user",
        "agent.machine_policy.conversation." + run.conversation_id,
        {harness.executor_id: "none"},
    )
    due(repo)
    await sweep_native_cancellations(harness.control, repo)
    due(repo)
    await sweep_native_cancellations(harness.control, repo)
    assert rows(repo)[session.key]["state"] == "stopped"


@pytest.mark.asyncio
async def test_deleted_conversation_and_expired_grant_latch_cancellation(
    bound, monkeypatch
):
    harness, repo = bound
    run, lease = generation(repo)
    task, session = await create(harness, repo, lease)
    assert WorkspaceRepository(repo.database).delete_conversation(
        "local-user", run.conversation_id
    )
    await sweep_native_cancellations(harness.control, repo)
    due(repo)
    await sweep_native_cancellations(harness.control, repo)
    assert (
        rows(repo)[task.key]["state"]
        == rows(repo)[session.key]["state"]
        == "stopped"
    )
    _, lease = generation(repo)
    now = int(time.time())
    intent = reserve(
        harness,
        repo,
        lease,
        ModelToolCall("expiring", "task", {"action": "create"}),
        expires=now + 1,
        dispatch=False,
    )
    monkeypatch.setattr(time, "time", lambda: now + 2)
    await sweep_native_cancellations(harness.control, repo)
    assert rows(repo)[intent.key]["state"] == "stopped"


@pytest.mark.asyncio
async def test_task_cancel_does_not_stop_external_session(bound):
    harness, repo = bound
    run, lease = generation(repo)
    task, session = await create(harness, repo, lease)
    external = await harness.control.session_coordinator.start_session(
        task_id=task.key, executor_id=harness.executor_id
    )
    repo.cancel_run(lease.owner, run.run_id)
    await sweep_native_cancellations(harness.control, repo)
    due(repo)
    await sweep_native_cancellations(harness.control, repo)
    assert rows(repo)[session.key]["state"] == "stopped"
    assert rows(repo)[task.key]["state"] == "cancel_pending"
    assert (
        await harness.control.task_service.read_task(task.key)
    ).status == "active"
    assert harness.executor.sessions.lookup(external["session_id"]) is not None


@pytest.mark.asyncio
async def test_reserved_session_cleanup_requires_original_binding_and_confirmed_absence(
    bound, monkeypatch
):
    from workgate.protocol.ids import new_executor_id

    harness, repo = bound
    _, lease = generation(repo)
    _, session = await create(harness, repo, lease)
    coordinator = harness.control.session_coordinator
    with pytest.raises(PermissionError, match="binding mismatch"):
        await coordinator.end_reserved_session(
            session.key, executor_id=new_executor_id()
        )
    assert harness.executor.sessions.lookup(session.key) is not None

    async def unavailable(*args, **kwargs):
        raise ExecutorTransportError(
            ProtocolErrorCode.EXECUTOR_OFFLINE, "offline"
        )

    monkeypatch.setattr(harness.control.executor_transport, "call", unavailable)
    result = await coordinator.end_session(session.key, force=True)
    assert result["force_released"] is True
    assert not await coordinator.end_reserved_session(
        session.key, executor_id=harness.executor_id
    )
    assert harness.executor.sessions.lookup(session.key) is not None


def test_cancellation_batch_is_bounded_and_retry_claims_are_not_immediately_reselected(
    bound,
):
    harness, repo = bound
    run, lease = generation(repo)
    intents = [
        reserve(
            harness,
            repo,
            lease,
            ModelToolCall(f"call-{i}", "task", {"action": "create"}),
        )
        for i in range(12)
    ]
    repo.cancel_run(lease.owner, run.run_id)
    first = repo.cancellation_batch((harness.executor_id,), limit=8)
    second = repo.cancellation_batch((harness.executor_id,), limit=8)
    assert len(first) == 8 and len(second) == 4
    assert {row["id"] for row in first + second} == {
        intent.id for intent in intents
    }
    assert not repo.cancellation_batch((harness.executor_id,), limit=8)
    repo.finish_cancellation(first[0]["id"], stopped=False)
    assert not repo.cancellation_batch((harness.executor_id,), limit=8)
    due(repo)
    assert len(repo.cancellation_batch((harness.executor_id,), limit=8)) == 8
