"""External event execution shares the unattended host, never native grants."""

import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control import hosted_chat_event_host as host
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.event_bindings import EventBindings
from workgate.hosted_chat.event_delivery_queue import EventDeliveryQueue
from workgate.hosted_chat.event_generation_queue import EventGenerations
from workgate.hosted_chat.event_source_keys import EventSourceKeys
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generation_supervisor import (
    GenerationCapacityExceeded,
)
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.provider_common import ModelToolCall


@pytest.fixture
def state(tmp_path):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    agent = AgentRepository(db).create(
        "alice",
        {
            "name": "Agent",
            "provider": "test-provider",
            "model": "test-model",
            "instructions": "Be precise.",
            "tools": ["task"],
            "subagents": {"enabled": True, "allowSelf": True},
        },
    )
    keys = EventSourceKeys(db)
    source = keys.mint("alice")["id"]
    queue = EventDeliveryQueue(db)
    events = EventGenerations(db)
    body = {
        "mode": "fire",
        "event": {"id": "event", "type": "update", "occurredAt": 123},
        "target": {"agentId": agent["id"]},
        "input": "Review progress",
    }
    return runtime, db, keys, source, queue, events, body


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fire", "continue", "revoke", "tool"])
async def test_event_host(state, mode):
    runtime, db, keys, source, queue, events, body = state
    store = GenerationStore(db)
    if mode == "continue":
        parent, _ = store.begin(
            "alice",
            {
                "text": "PRIVATE parent text",
                "agent_id": body["target"]["agentId"],
            },
        )
        lease = ActorLease.claim(db, "alice", parent.run_id)
        assert lease is not None
        store.finish(
            "alice",
            parent.run_id,
            status="complete",
            text="PRIVATE parent reply",
            actor_lease=lease,
        )
        lease.release()
        binding = EventBindings(db).register(
            "alice",
            source,
            "bind",
            actor_id="actor",
            parent_conversation_id=parent.conversation_id,
            parent_message_id=parent.response_message_id,
            target_agent_id=body["target"]["agentId"],
        )
        body = {**body, "mode": "continue", "bindingId": binding["id"]}
    ident = queue.enqueue("alice", source, "once", body)["id"]
    (item,) = events.admit()
    seen = []

    async def stream(profile, messages, **kwargs):
        seen.append(messages)
        assert profile.token == "test-secret"
        assert kwargs == {"max_tokens": 16384}
        assert messages == [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "Review progress"},
        ]
        if mode == "revoke":
            keys.revoke("alice", source)
        if mode == "tool":
            yield ModelToolCall("call", "task", {})
        else:
            yield "Done."

    assert await host.run_event_generation(
        runtime, db, item, stream_text=stream
    ) is (mode in {"fire", "continue"})
    assert not await host.run_event_generation(
        runtime, db, item, stream_text=stream
    )
    assert len(seen) == 1
    assert events.refresh() == 1
    generation = store.get("alice", item.run_id)
    assert generation is not None
    assert (
        generation.status
        == {
            "fire": "complete",
            "continue": "complete",
            "revoke": "interrupted",
            "tool": "error",
        }[mode]
    )
    if mode != "revoke":
        receipt = queue.get("alice", source, ident)
        assert receipt is not None
        assert receipt["status"] == ("dead" if mode == "tool" else "succeeded")
    assert "test-secret" not in str(store.events("alice", item.run_id))


@pytest.mark.asyncio
async def test_event_host_supervisor(state, monkeypatch, tmp_path):
    runtime, db, _, source, queue, events, body = state
    queue.enqueue("alice", source, "once", body)
    (item,) = events.admit()
    with pytest.raises(PermissionError):
        await host.run_event_generation(
            runtime_for(tmp_path / "other"), db, item
        )
    seen = []

    async def execute(runtime, database, current):
        seen.append(current)
        return True

    def exhausted(*args):
        raise GenerationCapacityExceeded("capacity")

    monkeypatch.setattr(host, "run_event_generation", execute)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.generation_supervisor, "start", exhausted)
        await host.tick_event_generations(runtime, db)
    assert not seen
    await host.tick_event_generations(runtime, db)
    assert await runtime.generation_supervisor.wait_idle(
        item.owner, item.run_id
    )
    assert seen == [item]
