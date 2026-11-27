"""Native intent admission adapted from the final durable task source tests."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.durable_tasks import DurableTaskRepository
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.provider_common import ModelToolCall
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.protocol.ids import new_executor_id, new_task_id

SCOPES = {"shell:read", "shell:write", "shell:execute"}
MACHINE = new_executor_id()
OTHER = new_executor_id()


@pytest.fixture
def bound(tmp_path):
    database = SQLiteDatabase(tmp_path)
    generation, _ = GenerationStore(database).begin(
        "alice", {"text": "native work"}
    )
    lease = ActorLease.claim(database, "alice", generation.run_id)
    assert lease is not None
    return database, generation, lease


def reserve(
    repo,
    lease,
    call,
    *,
    machine=MACHINE,
    scopes=SCOPES,
    expires=None,
    registered=(MACHINE, OTHER),
    **kwargs,
):
    return repo.reserve(
        lease,
        call,
        machine=machine,
        scopes=scopes,
        expires=expires,
        registered=registered,
        **kwargs,
    )


def claim(
    repo,
    lease,
    intent,
    call,
    *,
    scopes=SCOPES,
    expires=None,
    registered=(MACHINE, OTHER),
):
    return repo.claim(
        lease,
        intent,
        call,
        scopes=scopes,
        expires=expires,
        registered=registered,
    )


def task(call_id="task-call", **arguments):
    return ModelToolCall(call_id, "task", {"action": "create", **arguments})


def test_creation_intent_is_durable_before_native_tool_invocation(bound):
    database, generation, lease = bound
    repo = DurableTaskRepository(database)
    proposal = task(label="native")
    intent = reserve(repo, lease, proposal)
    assert intent.key.startswith("task_") and intent.state == "reserved"
    assert not repo.owned(
        "alice", generation.conversation_id, "task", intent.key
    )
    with database.connect() as db:
        row = db.execute("SELECT * FROM agent_durable_tasks").fetchone()
        assert row["state"] == "reserved" and row["native_id"] is None
        assert row["idempotency_key"] == intent.key
        assert row["run_id"] == generation.run_id
    assert reserve(repo, lease, proposal) == intent
    with pytest.raises(PermissionError, match="cannot change"):
        reserve(repo, lease, task(label="changed"))
    with pytest.raises(PermissionError, match="cannot change"):
        claim(repo, lease, intent, task(label="changed"))
    assert claim(repo, lease, intent, proposal)
    assert not claim(repo, lease, intent, proposal)
    with pytest.raises(PermissionError, match="evidence"):
        repo.bind(lease, intent, new_task_id())
    result = repo.bind(lease, intent, intent.key)
    assert result.state == "active" and result.native_id == intent.key
    assert repo.owned("alice", generation.conversation_id, "task", intent.key)
    assert reserve(repo, lease, proposal) == result


def test_crash_after_native_create_before_response_never_reoffers(bound):
    database, generation, lease = bound
    repo = DurableTaskRepository(database)
    proposal = task()
    intent = reserve(repo, lease, proposal)
    assert claim(repo, lease, intent, proposal)
    lease.release()
    reopened = SQLiteDatabase(database.root.parent)
    replacement = ActorLease.claim(reopened, "alice", generation.run_id)
    assert replacement is not None
    restarted = DurableTaskRepository(reopened)
    with pytest.raises(ActorLeaseLost):
        repo.bind(lease, intent, intent.key)
    resumed = reserve(restarted, replacement, proposal)
    assert (
        resumed.key == intent.key
        and resumed.state == "unknown"
        and resumed.native_id is None
    )
    assert not claim(restarted, replacement, resumed, proposal)
    # Only a positive, exact native read-only lookup permits this bind.
    confirmed = restarted.bind(replacement, resumed, resumed.key)
    assert confirmed.state == "active"


def test_cross_conversation_native_control_is_rejected(bound):
    database, generation, lease = bound
    repo = DurableTaskRepository(database)
    proposal = task()
    intent = reserve(repo, lease, proposal)
    assert claim(repo, lease, intent, proposal)
    repo.bind(lease, intent, intent.key)
    session = ModelToolCall(
        "session-call",
        "session_start",
        {"executor_id": OTHER, "task_id": intent.key},
    )
    session_intent = reserve(repo, lease, session, machine=OTHER)
    assert session_intent.key.startswith("sess_")
    assert claim(repo, lease, session_intent, session)
    repo.bind(lease, session_intent, session_intent.key)
    assert repo.owned(
        "alice",
        generation.conversation_id,
        "session",
        session_intent.key,
        machine=OTHER,
    )
    assert not repo.owned(
        "alice",
        generation.conversation_id,
        "session",
        session_intent.key,
        machine=MACHINE,
    )
    for owner in ("alice", "bob"):
        other, _ = GenerationStore(database).begin(
            owner, {"text": "another conversation"}
        )
        other_lease = ActorLease.claim(database, owner, other.run_id)
        assert other_lease is not None
        with pytest.raises(PermissionError, match="does not belong"):
            reserve(repo, other_lease, session, machine=OTHER)
        with pytest.raises(PermissionError, match="does not belong"):
            repo.bind(other_lease, intent, intent.key)
        assert not repo.owned(owner, other.conversation_id, "task", intent.key)


def test_foreign_native_identity_cannot_be_claimed(bound):
    database, _, lease = bound
    repo = DurableTaskRepository(database)
    for proposal in (
        task(task_id=new_task_id()),
        task(idempotency_key="external-key"),
        ModelToolCall(
            "session",
            "session_start",
            {
                "executor_id": MACHINE,
                "task_id": new_task_id(),
                "reserved_session_id": "external",
            },
        ),
        ModelToolCall(
            "session",
            "session_start",
            {"executor_id": OTHER, "task_id": new_task_id()},
        ),
    ):
        with pytest.raises(ValueError):
            reserve(repo, lease, proposal)
    with database.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM agent_durable_tasks").fetchone()[0]
            == 0
        )


def test_machine_grants_and_run_abort_are_durable(bound):
    database, generation, lease = bound
    repo = DurableTaskRepository(database)
    proposal = task()
    intent = reserve(repo, lease, proposal)
    assert claim(repo, lease, intent, proposal)
    other, _ = GenerationStore(database).begin("alice", {"text": "unrelated"})
    other_lease = ActorLease.claim(database, "alice", other.run_id)
    assert other_lease is not None
    independent = reserve(repo, other_lease, task())
    with pytest.raises(PermissionError):
        repo.cancel_run("bob", generation.run_id)
    repo.cancel_run("alice", generation.run_id)
    assert repo.is_run_cancelled(generation.run_id)
    assert not repo.is_run_cancelled(other.run_id)
    with pytest.raises(PermissionError, match="cancelled"):
        reserve(repo, lease, task("new"))
    with pytest.raises(PermissionError, match="cancelled"):
        claim(repo, lease, intent, proposal)
    # A late positive receipt retains the cancellation latch, never reactivates.
    assert repo.bind(lease, intent, intent.key).state == "cancel_pending"
    assert not repo.owned(
        "alice", generation.conversation_id, "task", intent.key
    )
    assert reserve(repo, other_lease, task()) == independent


def test_claim_rechecks_live_grant_and_original_expiration(bound):
    database, generation, lease = bound
    repo = DurableTaskRepository(database)
    workspace = WorkspaceRepository(database)
    proposal = task()
    with pytest.raises(PermissionError):
        reserve(repo, lease, proposal, registered=(OTHER,))
    with pytest.raises(PermissionError):
        reserve(repo, lease, proposal, scopes={"shell:read"})
    with pytest.raises(PermissionError):
        reserve(repo, lease, proposal, expires=int(time.time()) - 1)
    intent = reserve(repo, lease, proposal)
    workspace.save_setting(
        "alice",
        "agent.machine_policy.conversation." + generation.conversation_id,
        {MACHINE: "none"},
    )
    with pytest.raises(PermissionError):
        claim(repo, lease, intent, proposal)
    workspace.save_setting(
        "alice",
        "agent.machine_policy.conversation." + generation.conversation_id,
        {},
    )
    with pytest.raises(PermissionError):
        claim(repo, lease, intent, proposal, scopes={"shell:read"})
    with pytest.raises(PermissionError):
        claim(repo, lease, intent, proposal, registered=(OTHER,))
    assert claim(repo, lease, intent, proposal)
    assert not claim(repo, lease, intent, proposal)


def test_concurrent_reservation_and_dispatch_claim_are_single_winner(bound):
    database, _, lease = bound
    repo = DurableTaskRepository(database)
    proposal = task()
    with ThreadPoolExecutor(max_workers=8) as pool:
        intents = list(
            pool.map(lambda _: reserve(repo, lease, proposal), range(16))
        )
        claims = list(
            pool.map(
                lambda intent: claim(repo, lease, intent, proposal), intents
            )
        )
    assert len(set(intents)) == 1
    assert sum(claims) == 1


def test_replay_cannot_extend_original_grant_or_inherit_unrelated_chat(
    bound, monkeypatch
):
    database, generation, lease = bound
    repo = DurableTaskRepository(database)
    proposal = task()
    now = int(time.time())
    intent = reserve(repo, lease, proposal, expires=now + 10)
    other = WorkspaceRepository(database).create_conversation(
        "alice", {"title": "Other"}
    )["conversationId"]
    with pytest.raises(PermissionError, match="verified child lineage"):
        reserve(repo, lease, task("other"), authorization_conversation_id=other)
    monkeypatch.setattr(time, "time", lambda: now + 11)
    with pytest.raises(PermissionError, match="expired"):
        reserve(repo, lease, proposal, expires=now + 3600)
    with pytest.raises(PermissionError, match="expired"):
        claim(repo, lease, intent, proposal, expires=now + 3600)
    with database.connect() as db:
        assert (
            db.execute("SELECT state FROM agent_durable_tasks").fetchone()[0]
            == "reserved"
        )


def test_copy_intent_authorizes_both_executors_and_revokes_on_destination_loss(
    bound,
):
    database, generation, lease = bound
    repo = DurableTaskRepository(database)
    proposal = task()
    task_intent = reserve(repo, lease, proposal)
    assert claim(repo, lease, task_intent, proposal)
    repo.bind(lease, task_intent, task_intent.key)
    sessions = []
    for i, machine in enumerate((MACHINE, OTHER)):
        proposal = ModelToolCall(
            f"session-{i}",
            "session_start",
            {"executor_id": machine, "task_id": task_intent.key},
        )
        intent = reserve(repo, lease, proposal, machine=machine)
        assert claim(repo, lease, intent, proposal)
        repo.bind(lease, intent, intent.key)
        sessions.append(intent.key)
    copy_call = ModelToolCall(
        "copy",
        "session_copy",
        {
            "src_session_id": sessions[0],
            "src_path": "source",
            "dst_session_id": sessions[1],
            "dst_path": "dest",
        },
    )
    workspace = WorkspaceRepository(database)
    policy_key = (
        "agent.machine_policy.conversation." + generation.conversation_id
    )
    workspace.save_setting(
        "alice", policy_key, {MACHINE: "read", OTHER: "read"}
    )
    with pytest.raises(PermissionError, match="read-only"):
        reserve(repo, lease, copy_call, destination_machine=OTHER)
    workspace.save_setting(
        "alice", policy_key, {MACHINE: "read", OTHER: "write"}
    )
    with pytest.raises(PermissionError, match="shell:write"):
        reserve(
            repo,
            lease,
            copy_call,
            destination_machine=OTHER,
            scopes={"shell:read"},
        )
    intent = reserve(
        repo,
        lease,
        copy_call,
        destination_machine=OTHER,
        scopes={"shell:read", "shell:write"},
    )
    assert (
        intent.destination_session_id == sessions[1]
        and intent.destination_machine == OTHER
    )
    assert claim(
        repo, lease, intent, copy_call, scopes={"shell:read", "shell:write"}
    )
    repo.bind(lease, intent, intent.key)
    workspace.save_setting(
        "alice", policy_key, {MACHINE: "read", OTHER: "read"}
    )
    pending = repo.cancellation_batch((MACHINE, OTHER))
    assert {row["id"] for row in pending} == {intent.id}
