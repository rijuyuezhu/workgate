"""Durable scheduled admission without ambient execution grants."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.schedule_occurrences import ScheduleOccurrences
from workgate.hosted_chat.schedules import ScheduleConflict, ScheduleRepository
from workgate.hosted_chat.storage import _now


@pytest.fixture
def state(tmp_path):
    db = provider(runtime_for(tmp_path))
    agent = AgentRepository(db).create(
        "alice",
        {
            "name": "Research",
            "provider": "test-provider",
            "model": "test-model",
            "tools": ["task"],
        },
    )
    schedules = ScheduleRepository(db)
    data = {
        "name": "Review",
        "prompt": "Review progress",
        "agent_id": agent["id"],
        "timezone": "UTC",
        "cadence": {"frequency": "cron", "expression": "*/5 * * * *"},
        "clientRequestId": "first",
    }
    schedule = schedules.create("alice", data)
    return db, schedules, data, schedule["id"], ScheduleOccurrences(db)


def due(db, sid):
    minute = (_now() // 300_000_000 - 1) * 300_000
    with db.connect() as conn:
        conn.execute(
            "UPDATE webui_schedules SET next_run=? WHERE id=?", (minute, sid)
        )
    return minute


def test_atomic_claim_and_reserved_request(state):
    db, schedules, _, sid, occurrences = state
    previous = due(db, sid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: occurrences.claim_due(), range(4)))
    claimed = [claim for batch in results for claim in batch]
    assert len(claimed) == 1
    item = claimed[0]
    assert item.owner == "alice" and item.schedule_id == sid
    assert occurrences.pending() == claimed
    store = GenerationStore(db)
    assert store.native_admission("alice", item.run_id) is None
    with db.read_snapshot() as conn:
        record = conn.execute(
            "SELECT * FROM webui_schedule_runs WHERE id=?", (item.id,)
        ).fetchone()
        assert record["due_at"] == previous
        assert record["run_id"] == item.run_id
        assert (
            conn.execute(
                "SELECT next_run FROM webui_schedules WHERE id=?", (sid,)
            ).fetchone()[0]
            == previous + 300_000
        )
        saved = json.loads(
            conn.execute(
                "SELECT state FROM runs WHERE id=?", (item.run_id,)
            ).fetchone()[0]
        )
        assert saved["scheduleOccurrenceId"] == item.id
        assert saved["artifactsMode"] == ""
        assert "nativeAdmission" not in saved
    assert store.get("bob", item.run_id) is None
    with pytest.raises(ValueError, match="Internal"):
        store.begin(
            "alice", {"text": "forge", "clientRequestId": "schedule:" + item.id}
        )
    with pytest.raises(PermissionError), db.connect() as conn:
        store._begin_locked(
            conn,
            "bob",
            {"text": "forge", "clientRequestId": "schedule:" + item.id},
            schedule_occurrence_id=item.id,
        )
    lease = ActorLease.claim(
        db, "alice", item.run_id, schedule_occurrence_id=item.id
    )
    assert lease is not None
    store.finish(
        "alice", item.run_id, status="complete", text="Done", actor_lease=lease
    )
    lease.release()
    assert occurrences.refresh() == 1
    assert occurrences.refresh() == 0
    assert occurrences.pending() == []
    updated = schedules.get("alice", sid)
    assert updated["runCount"] == 1 and updated["failureCount"] == 0
    assert updated["lastRun"]["status"] == "success"


def test_paused_manual_and_cooldown(state):
    db, schedules, _, sid, occurrences = state
    schedules.update("alice", sid, {"enabled": False})
    assert occurrences.claim_due() == []
    with pytest.raises(KeyError):
        occurrences.claim_due(owner="bob", schedule_id=sid, manual=True)
    with pytest.raises(ValueError):
        occurrences.claim_due(manual=True)
    (item,) = occurrences.claim_due(owner="alice", schedule_id=sid, manual=True)
    assert (
        occurrences.claim_due(owner="alice", schedule_id=sid, manual=True) == []
    )
    lease = ActorLease.claim(
        db, "alice", item.run_id, schedule_occurrence_id=item.id
    )
    assert lease is not None
    GenerationStore(db).finish(
        "alice", item.run_id, status="error", error="test", actor_lease=lease
    )
    lease.release()
    assert occurrences.refresh() == 1
    assert (
        occurrences.claim_due(owner="alice", schedule_id=sid, manual=True) == []
    )
    assert schedules.get("alice", sid)["failureCount"] == 1
    assert schedules.get("alice", sid)["nextRunAt"] is None


def test_failed_dependency_does_not_block_healthy_claim(state):
    db, schedules, data, sid, occurrences = state
    good_agent = AgentRepository(db).create(
        "alice",
        {"name": "Another", "provider": "test-provider", "model": "test-model"},
    )["id"]
    other = schedules.create(
        "alice", {**data, "agent_id": good_agent, "clientRequestId": "second"}
    )["id"]
    AgentRepository(db).delete("alice", data["agent_id"])
    due(db, sid)
    due(db, other)
    items = occurrences.claim_due()
    assert len(items) == 1 and items[0].schedule_id == other
    failed = schedules.get("alice", sid)
    assert failed["runCount"] == failed["failureCount"] == 1
    assert failed["lastRun"]["error"] == "schedule_admission_failed"
    assert occurrences.refresh() == 0


def test_deleted_generation_is_accounted_once(state):
    db, schedules, _, sid, occurrences = state
    due(db, sid)
    (item,) = occurrences.claim_due()
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM conversations WHERE id=?", (item.conversation_id,)
        )
    assert occurrences.refresh() == 1
    assert occurrences.refresh() == 0
    assert schedules.get("alice", sid)["lastRun"]["status"] == "interrupted"


def test_execution_fence_and_lost_lease(state):
    db, schedules, _, sid, occurrences = state
    due(db, sid)
    (item,) = occurrences.claim_due()
    assert ActorLease.claim(db, "alice", item.run_id) is None
    assert (
        ActorLease.claim(
            db, "alice", item.run_id, schedule_occurrence_id="wrong"
        )
        is None
    )
    with pytest.raises(ScheduleConflict):
        schedules.delete("alice", sid)
    lease = ActorLease.claim(
        db, "alice", item.run_id, schedule_occurrence_id=item.id
    )
    assert lease is not None
    assert occurrences.pending() == []
    assert occurrences.refresh() == 0
    lease.release()
    assert (
        ActorLease.claim(
            db, "alice", item.run_id, schedule_occurrence_id=item.id
        )
        is None
    )
    assert occurrences.refresh() == 1
    run = GenerationStore(db).get("alice", item.run_id)
    assert run is not None and run.status == "interrupted"
    assert schedules.get("alice", sid)["lastRun"]["status"] == "interrupted"
    assert occurrences.refresh() == 0
    schedules.delete("alice", sid)
