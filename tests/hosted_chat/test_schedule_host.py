"""Scheduled execution never inherits the interactive tool or account grant."""

import asyncio
from dataclasses import replace

import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control import hosted_chat_schedule_host as host
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generation_supervisor import (
    GenerationCapacityExceeded,
)
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.provider_common import (
    ModelFinish,
    ModelThought,
    ModelToolCall,
    ModelUsage,
)
from workgate.hosted_chat.schedule_occurrences import ScheduleOccurrences
from workgate.hosted_chat.schedules import ScheduleRepository


@pytest.fixture
def state(tmp_path):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    agent = AgentRepository(db).create(
        "alice",
        {
            "name": "Research",
            "provider": "test-provider",
            "model": "test-model",
            "instructions": "Review carefully.",
            "tools": ["task", "web_search"],
        },
    )
    schedules = ScheduleRepository(db)
    data = {
        "name": "Review",
        "prompt": "Review progress",
        "agent_id": agent["id"],
        "timezone": "UTC",
        "cadence": {"frequency": "daily", "hour": 9, "minute": 0},
        "clientRequestId": "first",
    }
    schedule = schedules.create("alice", data)
    occurrences = ScheduleOccurrences(db)
    (item,) = occurrences.claim_due(
        owner="alice", schedule_id=schedule["id"], manual=True
    )
    return runtime, db, occurrences, item, agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "tool",
        "overflow",
        "revoked",
        "provider_error",
        "cancel",
        "changed_before",
        "lost",
    ],
)
async def test_scheduled_model_only(state, monkeypatch, mode):
    runtime, db, occurrences, item, agent = state
    seen = []

    async def stream(profile, messages, **kwargs):
        seen.append(messages)
        assert profile.token == "test-secret"
        assert kwargs == {"max_tokens": 16384}
        assert messages[0]["content"] == "Review carefully."
        assert messages[-1]["content"] == "Review progress"
        if mode == "cancel":
            raise asyncio.CancelledError()
        if mode == "provider_error":
            raise RuntimeError("do not leak test-secret")
        if mode == "revoked":
            AgentRepository(db).update(
                "alice", agent["id"], {"name": "Changed"}
            )
        if mode == "lost":
            with db.connect() as conn:
                conn.execute(
                    "DELETE FROM generation_actor_leases WHERE run_id=?",
                    (item.run_id,),
                )
        if mode == "tool":
            yield ModelToolCall("call", "task", {})
        else:
            yield ModelThought("thinking", "Thought.")
            yield "Answer."
            yield ModelUsage("openai", 50, 10)
            yield ModelFinish("openai", "stop")

    if mode == "changed_before":
        AgentRepository(db).update("alice", agent["id"], {"name": "Changed"})
    if mode == "overflow":
        monkeypatch.setattr(host, "_MAX_OUTPUT_BYTES", 2)
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await host.run_scheduled_generation(
                runtime, db, item, stream_text=stream
            )
    else:
        assert await host.run_scheduled_generation(
            runtime, db, item, stream_text=stream
        ) is (mode == "success")
    assert (
        await host.run_scheduled_generation(
            runtime, db, item, stream_text=stream
        )
        is False
    )
    assert len(seen) == (0 if mode == "changed_before" else 1)
    assert occurrences.refresh() == 1
    result = GenerationStore(db).get("alice", item.run_id)
    assert result is not None
    assert result.status == (
        "complete"
        if mode == "success"
        else "interrupted"
        if mode in {"cancel", "lost"}
        else "error"
    )
    events = GenerationStore(db).events("alice", item.run_id)
    assert "test-secret" not in str(events)
    if mode == "success":
        assert events[-1][1]["responseMessage"]["text"] == "Answer."
    assert ActorLease.claim(db, "alice", item.run_id) is None


@pytest.mark.asyncio
async def test_supervisor_pending_capacity_and_dispatch(state, monkeypatch):
    runtime, db, occurrences, item, _ = state
    submitted = []

    async def execute(runtime, database, occurrence):
        submitted.append(occurrence)
        return True

    def exhausted(*args):
        raise GenerationCapacityExceeded("capacity")

    monkeypatch.setattr(host, "run_scheduled_generation", execute)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.generation_supervisor, "start", exhausted)
        await host.tick_scheduled_generations(runtime, db)
    assert not submitted and occurrences.pending() == [item]
    await host.tick_scheduled_generations(runtime, db)
    assert await runtime.generation_supervisor.wait_idle(
        item.owner, item.run_id
    )
    assert submitted == [item]


@pytest.mark.asyncio
async def test_mismatched_runtime_and_occurrence(state, tmp_path):
    runtime, db, _, item, _ = state
    other = runtime_for(tmp_path / "other")
    with pytest.raises(PermissionError):
        await host.run_scheduled_generation(other, db, item)
    assert not await host.run_scheduled_generation(
        runtime, db, replace(item, id="fake")
    )
