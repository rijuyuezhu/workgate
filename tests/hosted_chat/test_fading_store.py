"""Fading is a durable provider projection, not a native effect or history edit."""

import copy
import json
from dataclasses import replace

import pytest

from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.fading_projection import (
    project_faded_provider_messages,
)
from workgate.hosted_chat.fading_store import update_root_fading
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.provider_common import ProviderConfig
from workgate.hosted_chat.provider_profiles import ProviderRepository


@pytest.fixture
def bound(tmp_path):
    db = SQLiteDatabase(tmp_path)
    ProviderRepository(db, CredentialCipher(db)).save(
        {
            "name": "channel",
            "provider": "openai",
            "base_url": "https://example.invalid/v1",
            "models": ["m", "other"],
            "model_contexts": {"m": 4096},
            "api_key": "secret",
        }
    )
    store = GenerationStore(db)
    job, _ = store.begin(
        "alice", {"text": "read", "endpoint": "channel", "model": "m"}
    )
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    state = AgentLoopState(
        AgentContext(
            ProviderConfig(
                "openai",
                "m",
                "https://example.invalid",
                "secret",
                channel="channel",
            ),
            [],
        ),
        [{"role": "user", "content": "read"}],
    )
    return store, job, lease, state


def update(lease, state):
    return update_root_fading(
        lease, state, state.messages, check_grant=lambda: None
    )


def test_fading_hysteresis_replay_and_compaction_reset(bound):
    store, job, lease, state = bound
    state.messages += [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "one",
                    "function": {
                        "name": "read",
                        "arguments": json.dumps({"path": "x" * 8000}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "one", "content": "detail " * 12000},
        {"role": "assistant", "content": "Read it."},
    ]
    full = copy.deepcopy(state.messages)
    tier = update(lease, state)
    assert tier is not None and tier["masked"]
    projected = project_faded_provider_messages(state.messages, tier)
    assert len(projected[2]["content"]) < len(full[2]["content"])
    assert state.messages == full
    state.messages = [{"role": "user", "content": "short"}]
    assert update(lease, state) == tier
    with store.database.connect() as db:
        db.execute(
            "UPDATE runs SET state=json_set(state,'$.compaction',json(?)) WHERE id=?",
            (json.dumps({"fingerprint": "new-summary"}), job.run_id),
        )
    reset = update(lease, state)
    assert reset is not None and not reset["masked"]
    assert reset["budgetTokens"] >= tier["budgetTokens"]
    lease.release()
    with pytest.raises(ActorLeaseLost):
        update(lease, state)
    fresh = ActorLease.claim(store.database, "alice", job.run_id)
    assert fresh is not None
    assert update(fresh, state) == reset


def test_fading_seeds_only_exact_branch_and_model(bound):
    store, job, lease, state = bound
    state.messages = [{"role": "user", "content": "detail " * 12000}]
    tier = update(lease, state)
    assert tier is not None and tier["masked"]
    assert store.finish(
        "alice", job.run_id, status="complete", text="done", actor_lease=lease
    )
    follow, _ = store.begin(
        "alice",
        {
            "text": "next",
            "conversationId": job.conversation_id,
            "parentMessageId": job.response_message_id,
        },
    )
    actor = ActorLease.claim(store.database, "alice", follow.run_id)
    assert actor is not None
    state.messages = [{"role": "user", "content": "next"}]
    assert update(actor, state) == tier
    assert store.finish(
        "alice",
        follow.run_id,
        status="complete",
        text="done",
        actor_lease=actor,
    )
    fork, _ = store.begin(
        "alice",
        {
            "text": "fork",
            "conversationId": job.conversation_id,
            "parentMessageId": "00000000-0000-0000-0000-000000000000",
        },
    )
    fresh = ActorLease.claim(store.database, "alice", fork.run_id)
    assert fresh is not None
    unmasked = update(fresh, state)
    assert unmasked is not None and not unmasked["masked"]
    assert update(replace(fresh, detached_task_id="child"), state) is None
    assert update(replace(fresh, wakeup_task_id="wake"), state) is None
    state.context.profile = replace(state.context.profile, model="other")
    assert update(fresh, state) is None


def test_fading_commit_revocation_rolls_back(bound):
    store, job, lease, state = bound
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        update_root_fading(lease, state, state.messages, check_grant=check)
    with store.database.read_snapshot() as db:
        run = json.loads(
            db.execute(
                "SELECT state FROM runs WHERE id=?", (job.run_id,)
            ).fetchone()[0]
        )
    assert "contextFading" not in run


def test_fading_unknown_context_is_not_guessed(bound):
    store, job, lease, state = bound
    assert store.finish(
        "alice", job.run_id, status="complete", text="done", actor_lease=lease
    )
    other, _ = store.begin(
        "alice",
        {
            "text": "next",
            "conversationId": job.conversation_id,
            "model": "other",
            "endpoint": "channel",
        },
    )
    actor = ActorLease.claim(store.database, "alice", other.run_id)
    assert actor is not None
    state.context.profile = replace(state.context.profile, model="other")
    assert update(actor, state) is None
