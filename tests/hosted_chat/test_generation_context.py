"""Admitted generation identity, text branch and provider configuration."""

from dataclasses import replace

import pytest

from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generation_context import GenerationContextResolver
from workgate.hosted_chat.generations import GenerationStore


@pytest.fixture
def resolver(tmp_path):
    db = SQLiteDatabase(tmp_path)
    value = GenerationContextResolver(db, "alice")
    value.providers.save(
        {
            "name": "named",
            "provider": "openai",
            "base_url": "https://models.invalid/v1",
            "api_key": "secret-key",
            "models": ["parent", "child"],
            "model_sampling": {
                "parent": {"temperature": 0.7, "max_tokens": 400},
                "child": {"top_p": 0.5},
            },
        }
    )
    return value


@pytest.mark.asyncio
async def test_ordinary_context_uses_exact_branch_and_sampling(resolver):
    store = GenerationStore(resolver.database)
    first, _ = store.begin(
        "alice", {"provider": "named", "model": "parent", "text": "first"}
    )
    lease = ActorLease.claim(resolver.database, "alice", first.run_id)
    assert lease is not None
    assert store.finish(
        "alice",
        first.run_id,
        status="complete",
        text="first answer",
        actor_lease=lease,
    )
    lease.release()
    sibling, _ = store.begin(
        "alice",
        {
            "conversationId": first.conversation_id,
            "parentMessageId": first.user_message_id,
            "text": "alternate",
        },
    )
    lease = ActorLease.claim(resolver.database, "alice", sibling.run_id)
    assert lease is not None
    assert store.finish(
        "alice",
        sibling.run_id,
        status="complete",
        text="alternate answer",
        actor_lease=lease,
    )
    lease.release()
    generation, _ = store.begin(
        "alice",
        {
            "conversationId": first.conversation_id,
            "parentMessageId": first.response_message_id,
            "text": "chosen branch",
            "lsmSampling": {"temperature": 0},
        },
    )
    context = await resolver.root(generation)
    assert context.profile.channel == "named"
    assert context.profile.model == "parent"
    assert context.parameters == {"temperature": 0, "max_tokens": 400}
    assert context.tools == [] and context.agent is None
    assert context.profile.token == "secret-key"
    assert "secret-key" not in repr(context)
    assert resolver.history(generation, context) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "chosen branch"},
    ]
    assert (
        await resolver.child(generation, None)
    ).parameters == context.parameters
    with pytest.raises(PermissionError, match="model identity"):
        await resolver.root(replace(generation, model="child"))
    with pytest.raises(PermissionError, match="model identity"):
        await GenerationContextResolver(resolver.database, "bob").root(
            generation
        )
    with resolver.database.connect() as db:
        db.execute(
            "UPDATE messages SET parent_id=? WHERE id=?",
            (generation.user_message_id, generation.user_message_id),
        )
    with pytest.raises(PermissionError, match="cyclic"):
        resolver.history(generation, context)


@pytest.mark.asyncio
async def test_saved_agent_context_and_child_revision(resolver):
    agents = AgentRepository(resolver.database)
    parent = agents.create(
        "alice",
        {
            "provider": "named",
            "model": "parent",
            "instructions": "parent instructions",
            "model_parameters": {"temperature": 0, "maxTokens": 300},
            "recursion_limit": 7,
            "end_after_tools": True,
        },
    )
    child = agents.create(
        "alice",
        {
            "provider": "named",
            "model": "child",
            "instructions": "child instructions",
            "model_parameters": {"topP": 0},
        },
    )
    generation, _ = GenerationStore(resolver.database).begin(
        "alice", {"agent_id": parent["id"], "text": "question"}
    )
    context = await resolver.root(generation)
    assert context.agent == parent
    assert context.recursion_limit == 7 and context.tool_end
    assert context.parameters == {"temperature": 0, "max_tokens": 300}
    assert resolver.history(generation, context) == [
        {"role": "system", "content": "parent instructions"},
        {"role": "user", "content": "question"},
    ]
    nested = await resolver.child(generation, child)
    assert nested.profile.model == "child"
    assert nested.parameters == {"top_p": 0}
    assert nested.agent == child
    with pytest.raises(PermissionError, match="ordinary chat"):
        await resolver.child(generation, None)
    with pytest.raises(PermissionError, match="model identity"):
        await resolver.child(generation, {**child, "model": "parent"})
    with pytest.raises(PermissionError, match="no longer owned"):
        await resolver.child(generation, {**child, "id": "other"})
    agents.update("alice", child["id"], {"instructions": "changed"})
    with pytest.raises(PermissionError, match="revision changed"):
        await resolver.child(generation, child)
    agents.update("alice", parent["id"], {"instructions": "changed"})
    with pytest.raises(PermissionError, match="revision changed"):
        await resolver.root(generation)


@pytest.mark.asyncio
async def test_legacy_channel_alias_is_canonical_at_admission(resolver):
    resolver.providers.save(
        {
            "provider": "openai",
            "base_url": "https://models.invalid/v1",
            "api_key": "legacy-key",
            "models": ["legacy-model"],
        }
    )
    agent = AgentRepository(resolver.database).create(
        "alice", {"provider": "ollama", "model": "legacy-model"}
    )
    generation, _ = GenerationStore(resolver.database).begin(
        "alice", {"text": "question", "agent_id": agent["id"]}
    )
    assert generation.provider == "openai"
    context = await resolver.root(generation)
    assert (
        context.profile.channel == "openai"
        and context.profile.token == "legacy-key"
    )
