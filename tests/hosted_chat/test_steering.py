"""Private foreground steering admission adapted from pinned source tests."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.steering import (
    QUEUE_LIMIT,
    SteeringStore,
    SteerRejection,
    merged_text,
)


@pytest.fixture
def context(tmp_path):
    database = SQLiteDatabase(tmp_path)
    generations = GenerationStore(database)
    job, _ = generations.begin("alice", {"text": "Question"})
    return generations, SteeringStore(database), job


def enqueue(steering, job, cid="stable", text="New direction", **kw):
    return steering.enqueue(
        "alice",
        {
            "conversationId": job.conversation_id,
            "generationCreatedAt": job.generation_created_at,
            "clientSteerId": cid,
            "text": text,
            **kw,
        },
        preempt_capable=True,
        validate_submission=lambda text: None,
    )


def test_queue_receipt_and_terminal_replay(context):
    generations, steering, job = context
    one = enqueue(steering, job, "one", quotes=["  excerpt  "])
    two = enqueue(steering, job, "two", preempt=True)
    assert one["position"] == 1 and two["position"] == 2
    assert one["quotesAccepted"] and steering.has_preempt("alice", job.run_id)
    assert not steering.has_preempt("bob", job.run_id)
    assert steering.pending("bob", job.run_id) == []
    pending = steering.pending("alice", job.run_id)
    assert merged_text(pending[0]) == "> excerpt\n\nNew direction"
    assert enqueue(steering, job, "one", quotes=["excerpt"])["replayed"]
    with pytest.raises(SteerRejection, match="STEER_IDEMPOTENCY_CONFLICT"):
        enqueue(steering, job, "one", text="different")
    lease = ActorLease.claim(generations.database, "alice", job.run_id)
    assert lease is not None
    assert generations.finish(
        "alice", job.run_id, status="complete", actor_lease=lease
    )
    final = generations.events("alice", job.run_id)[-1][1]
    assert [i["steerId"] for i in final["pendingSteers"]] == [
        one["steerId"],
        two["steerId"],
    ]
    assert steering.pending("alice", job.run_id) == []
    successor, _ = generations.begin(
        "alice", {"conversationId": job.conversation_id, "text": "next"}
    )
    replay = enqueue(steering, job, "two", preempt=True)
    assert replay["replayed"] and replay["settled"] and replay["leftover"]
    assert steering.pending("alice", successor.run_id) == []
    with pytest.raises(SteerRejection, match="RUN_REPLACED"):
        enqueue(steering, job, "new")
    assert steering.mutate(
        "alice",
        {
            "conversationId": job.conversation_id,
            "generationCreatedAt": job.generation_created_at,
            "steerId": two["steerId"],
            "clientSteerId": "two",
        },
        arm=False,
        preempt_capable=True,
    ) == {"removed": True, "replayed": True}
    assert not enqueue(steering, job, "two", preempt=True)["leftover"]


def test_arm_cancel_and_owner_epoch(context):
    _, steering, job = context
    one = enqueue(steering, job, "one")
    body = {
        "conversationId": job.conversation_id,
        "steerId": one["steerId"],
        "clientSteerId": "one",
    }
    assert steering.mutate("bob", body, arm=False, preempt_capable=True) == {
        "removed": False
    }
    assert steering.mutate("alice", body, arm=True, preempt_capable=False) == {
        "armed": False,
        "code": "PREEMPT_UNSUPPORTED",
    }
    assert steering.mutate("alice", body, arm=True, preempt_capable=True) == {
        "armed": True,
        "preemptRevision": 2,
    }
    assert steering.mutate("alice", body, arm=True, preempt_capable=True) == {
        "armed": False
    }
    assert enqueue(steering, job, "one")["preempt"]
    with pytest.raises(SteerRejection, match="RUN_REPLACED"):
        steering.mutate(
            "alice",
            {**body, "generationCreatedAt": job.generation_created_at + 1},
            arm=False,
            preempt_capable=True,
        )
    assert steering.mutate("alice", body, arm=False, preempt_capable=True) == {
        "removed": True
    }
    assert steering.mutate("alice", body, arm=False, preempt_capable=True) == {
        "removed": True,
        "replayed": True,
    }
    assert not steering.has_preempt("alice", job.run_id)


def test_concurrent_receipts_and_queue_limit(context):
    _, steering, job = context

    def submit(i):
        try:
            return enqueue(steering, job, f"id-{i % 12}", f"text-{i % 12}")
        except SteerRejection as error:
            assert error.body["code"] == "STEER_QUEUE_FULL"
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(submit, range(60)))
    assert len({i["steerId"] for i in results if i}) == QUEUE_LIMIT
    assert len(steering.pending("alice", job.run_id)) == QUEUE_LIMIT


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"conversationId": "new"}, "INVALID_CONVERSATION"),
        ({"text": "\x00  "}, "EMPTY_TEXT"),
        ({"text": "x" * 16001}, "STEER_TOO_LONG"),
        ({"clientSteerId": "bad id"}, "INVALID_CLIENT_STEER_ID"),
        ({"files": [{"file_id": "x"}]}, "INVALID_FILES"),
        ({"generationCreatedAt": True}, "INVALID_GENERATION_IDENTITY"),
    ],
)
def test_reject_invalid_submission(context, patch, code):
    _, steering, job = context
    with pytest.raises(SteerRejection, match=code):
        enqueue(steering, job, **patch)
    assert steering.pending("alice", job.run_id) == []


def test_content_policy_checks_combined_submission(context):
    _, steering, job = context
    seen = []

    def validate(text):
        seen.append(text)
        if text.startswith(">"):
            raise ValueError("blocked combined submission")

    with pytest.raises(ValueError, match="blocked combined submission"):
        steering.enqueue(
            "alice",
            {
                "conversationId": job.conversation_id,
                "text": "body",
                "quotes": ["quote"],
            },
            preempt_capable=True,
            validate_submission=validate,
        )
    assert seen == ["body", "quote", "> quote\n\nbody"]
    assert steering.pending("alice", job.run_id) == []


def test_event_limit_rolls_back_queue_and_arm(context, monkeypatch):
    from workgate.hosted_chat import steering as module

    generations, steering, job = context
    monkeypatch.setattr(module, "EVENT_LIMIT", 1)
    with pytest.raises(SteerRejection, match="STEER_EVENT_LIMIT"):
        enqueue(steering, job)
    assert not steering.pending("alice", job.run_id)
    monkeypatch.setattr(module, "EVENT_LIMIT", 2)
    one = enqueue(steering, job)
    with pytest.raises(SteerRejection, match="STEER_EVENT_LIMIT"):
        steering.mutate(
            "alice",
            {"conversationId": job.conversation_id, "steerId": one["steerId"]},
            arm=True,
            preempt_capable=True,
        )
    assert not steering.has_preempt("alice", job.run_id)
    assert len(generations.events("alice", job.run_id)) == 2


@pytest.mark.asyncio
async def test_approval_abort_preserves_leftovers_without_resurrecting_claim(
    context,
):
    import json

    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import (
        ModelToolCall,
        ProviderConfig,
    )

    generations, steering, job = context
    one = enqueue(steering, job)
    lease = ActorLease.claim(generations.database, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    calls = [ModelToolCall("q", "ask_user_question", {"question": "Choose?"})]
    state = AgentLoopState(
        AgentContext(
            ProviderConfig("openai", "m", "https://example.invalid", "secret"),
            [],
        ),
        [],
        pending_calls=calls,
        tool_step_started=True,
    )
    await checkpoint.save(state, calls)
    store = ApprovalClaimStore(generations.database)
    pending = store.pause_question(checkpoint, calls[0])
    with pytest.raises(SteerRejection, match="RUN_PAUSED"):
        enqueue(steering, job, "new")
    assert enqueue(steering, job)["replayed"]
    assert store.abort_pending("alice", job.run_id, pending["actionId"])
    assert (
        generations.events("alice", job.run_id)[-1][1]["pendingSteers"][0][
            "steerId"
        ]
        == one["steerId"]
    )
    with generations.database.read_snapshot() as db:
        saved = json.loads(
            db.execute(
                "SELECT state FROM runs WHERE id=?", (job.run_id,)
            ).fetchone()[0]
        )
    assert "pendingAction" not in saved and "approvalCheckpointId" not in saved
