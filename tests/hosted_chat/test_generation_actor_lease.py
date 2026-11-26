"""Owner-scoped actor lease tests adapted from the pinned upstream source."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.hosted_chat import generation_actor_lease
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
    active_run_ids,
)


@pytest.fixture
def context(tmp_path):
    first, second = SQLiteDatabase(tmp_path), SQLiteDatabase(tmp_path)
    with first.connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id,owner) VALUES('chat','local')"
        )
        conn.execute(
            "INSERT INTO runs(id,conversation_id,status) VALUES('run','chat','running')"
        )
    return first, second, "run"


def test_one_actor_claim_across_controller_db_instances(context):
    first, second, run_id = context

    def race(index):
        return ActorLease.claim(first if index % 2 else second, "local", run_id)

    with ThreadPoolExecutor(max_workers=20) as pool:
        actors = [a for a in pool.map(race, range(32)) if a is not None]
    assert len(actors) == 1
    winner = actors[0]
    assert winner.is_current()
    assert run_id in active_run_ids(second)
    assert winner.renew()
    assert not ActorLease.claim(second, "other", run_id)
    with first.connect() as conn:
        conn.execute(
            "UPDATE generation_actor_leases SET expires_at=0 WHERE run_id=?",
            (run_id,),
        )
    replacement = ActorLease.claim(second, "local", run_id)
    assert replacement and replacement.token != winner.token
    with pytest.raises(ActorLeaseLost, match="lease"):
        winner.assert_current()
    with first.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ActorLeaseLost, match="lease"):
            winner.assert_current(conn)
        replacement.assert_current(conn)
        conn.execute("UPDATE runs SET status='complete' WHERE id=?", (run_id,))
    assert not replacement.renew()
    winner.release()  # old actor must not delete the new actor's lease
    with first.connect() as conn:
        assert (
            conn.execute(
                "SELECT token FROM generation_actor_leases WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == replacement.token
        )
    replacement.release()
    assert run_id not in active_run_ids(first)


@pytest.mark.asyncio
async def test_heartbeat_stops_if_run_lease_changes(context, monkeypatch):
    first, second, run_id = context
    lease = ActorLease.claim(first, "local", run_id)
    assert lease is not None
    monkeypatch.setattr(generation_actor_lease, "_RENEW_S", 0.01)
    with first.connect() as conn:
        conn.execute(
            "UPDATE generation_actor_leases SET expires_at=0 WHERE run_id=?",
            (run_id,),
        )
    await asyncio.wait_for(lease.keep_alive(), 1)
    assert not lease.is_current()
    assert not lease.renew()
    replacement = ActorLease.claim(second, "local", run_id)
    assert replacement is not None
    lease.release()
    assert replacement.is_current()
    replacement.release()


def test_owner_and_run_status_fence_all_operations(context):
    first, second, run_id = context
    assert ActorLease.claim(first, "other", run_id) is None
    assert ActorLease.claim(first, "local", "missing") is None
    lease = ActorLease.claim(first, "local", run_id)
    assert lease is not None
    with first.connect() as conn:
        conn.execute("UPDATE conversations SET owner='other' WHERE id='chat'")
    assert not lease.is_current()
    assert not lease.renew()
    assert active_run_ids(second) == set()
    lease.release()
    with first.connect() as conn:
        conn.execute("UPDATE runs SET status='complete' WHERE id=?", (run_id,))
    assert ActorLease.claim(second, "other", run_id) is None
