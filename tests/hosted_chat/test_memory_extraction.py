"""Bounded and owner-fenced Memory Agent proposals; never real model traffic."""

import asyncio
import json

import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.memory import MemoryRepository
from workgate.hosted_chat.memory_extraction import (
    _bounded_window,
    extract_memory_proposals,
    plan_extraction,
)
from workgate.hosted_chat.memory_protection import MemoryContentBlocked
from workgate.hosted_chat.provider_common import ModelToolCall
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.hosted_chat.tokenizer import count_tokens


def setup(
    tmp_path, *, text="Remember I like short answers.", agent_config=None
):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    repo = WorkspaceRepository(db)
    body = {"text": text, "provider": "test-provider", "model": "test-model"}
    if agent_config:
        agent = AgentRepository(db).create(
            "alice",
            {
                "provider": "test-provider",
                "model": "test-model",
                **agent_config,
            },
        )
        body["agent_id"] = agent["id"]
    generation, _ = GenerationStore(db).begin("alice", body)
    lease = ActorLease.claim(db, "alice", generation.run_id)
    assert lease is not None
    return repo, generation, lease


def configure(repo, **changes):
    config = {
        "agent": {
            "enabled": True,
            "provider": "test-provider",
            "model": "test-model",
        },
        "validKeys": ["style"],
        **changes,
    }
    repo.save_setting("alice", "webui.memory.config", config)
    return config


@pytest.mark.asyncio
async def test_extraction_enabled_only_and_never_mutates(tmp_path):
    repo, generation, lease = setup(tmp_path)
    store = MemoryRepository(lease.database)
    store.write("alice", "existing", "Existing personal memory.")
    store.write("bob", "other", "Other owner marker.")
    assert plan_extraction(lease) is None

    async def stream(profile, messages, **kwargs):
        assert profile.model == "test-model"
        assert "Remember I like short answers." in messages[1]["content"]
        assert "Existing personal memory." in messages[0]["content"]
        assert "Other owner marker." not in repr(messages)
        assert [tool["function"]["name"] for tool in kwargs["tools"]] == [
            "set_memory",
            "delete_memory",
        ]
        assert kwargs["max_tokens"] == 1024
        yield "unused model text"
        yield ModelToolCall(
            "one", "set_memory", {"key": "style", "value": "Short answers."}
        )

    assert (
        await extract_memory_proposals(
            lease, check_grant=lambda: None, stream_tools=stream
        )
        is None
    )
    configure(repo)
    proposals = await extract_memory_proposals(
        lease, check_grant=lambda: None, stream_tools=stream
    )
    assert proposals is not None and len(proposals.calls) == 1
    assert len(store.list("alice")) == 1
    assert "Short answers." not in repr(proposals)
    repo.save_setting(
        "alice",
        "conversation.permissions." + generation.conversation_id,
        {"memory": False},
    )
    assert plan_extraction(lease) is None
    repo.save_setting(
        "alice",
        "conversation.permissions." + generation.conversation_id,
        {"memory": True},
    )
    repo.save_setting("alice", "user.memories", False)
    assert plan_extraction(lease) is None


def test_extraction_agent_scope_policy_and_window(tmp_path):
    repo, generation, lease = setup(
        tmp_path, agent_config={"memory_scope": "agent"}
    )
    aid = generation.agent_id
    assert aid is not None
    store = MemoryRepository(lease.database)
    store.write("alice", "shared", "Shared marker.")
    store.write("alice", "scoped", "Scoped marker.", agent_id=aid)
    store.write("alice", "secret", "Bearer private-token", agent_id=aid)
    model = AgentRepository(lease.database).create(
        "alice",
        {
            "provider": "test-provider",
            "model": "test-model",
            "instructions": "Extraction instructions.",
        },
    )
    configure(
        repo, agent={"enabled": True, "id": model["id"]}, maxInputTokens=32
    )
    repo.save_setting(
        "alice",
        "webui.filters.config",
        {
            "memories": {
                "pii": {"action": "block", "starterPatterns": ["bearer_header"]}
            }
        },
    )
    plan = plan_extraction(lease)
    assert plan is not None and plan.partition == aid
    assert "Scoped marker." in repr(
        plan.messages
    ) and "Shared marker." not in repr(plan.messages)
    assert "private-token" not in repr(plan.messages)
    assert "Extraction instructions." in repr(plan.messages)
    assert count_tokens(plan.messages[1]["content"]) <= 32
    assert (
        count_tokens(
            _bounded_window(
                [
                    {"role": "user", "content": "word " * 1000},
                    {"role": "assistant", "content": "response"},
                    {"role": "tool", "content": "secret tool"},
                ],
                window=1,
                tokens=32,
            )
        )
        <= 32
    )
    configure(repo, agent={"enabled": True, "id": "missing"})
    assert plan_extraction(lease) is None
    configure(repo, maxInputTokens=31)
    with pytest.raises(ValueError, match="window"):
        plan_extraction(lease)
    configure(repo, validKeys="invalid")
    with pytest.raises(ValueError, match="keys"):
        plan_extraction(lease)
    configure(repo)
    AgentRepository(lease.database).update(
        "alice", aid, {"instructions": "revised"}
    )
    with pytest.raises(PermissionError, match="revision"):
        plan_extraction(lease)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "unknown",
        "wrong_key",
        "too_many",
        "revoke",
        "protected",
        "duplicate",
        "timeout",
        "event_limit",
    ],
)
async def test_extraction_fails_closed(tmp_path, monkeypatch, mode):
    from workgate.hosted_chat import memory_extraction

    repo, _, lease = setup(tmp_path)
    configure(repo)
    if mode == "protected":
        repo.save_setting(
            "alice",
            "webui.filters.config",
            {
                "memories": {
                    "pii": {
                        "action": "block",
                        "starterPatterns": ["bearer_header"],
                    }
                }
            },
        )
    if mode == "timeout":
        monkeypatch.setattr(memory_extraction, "_TIMEOUT_S", 0.01)
    if mode == "event_limit":
        monkeypatch.setattr(memory_extraction, "_MAX_EVENTS", 1)
    closed = []

    async def stream(*args, **kwargs):
        try:
            if mode == "timeout":
                await asyncio.Event().wait()
            if mode == "event_limit":
                yield "one"
                yield "two"
            if mode == "revoke":
                repo.save_setting("alice", "user.memories", False)
            if mode == "unknown":
                yield ModelToolCall("one", "task", {})
            elif mode == "wrong_key":
                yield ModelToolCall(
                    "one", "set_memory", {"key": "other", "value": "value"}
                )
            elif mode == "too_many":
                for i in range(9):
                    yield ModelToolCall(
                        str(i), "set_memory", {"key": "style", "value": "value"}
                    )
            elif mode == "protected":
                yield ModelToolCall(
                    "one",
                    "set_memory",
                    {"key": "style", "value": "Bearer private-token"},
                )
            elif mode == "duplicate":
                for _ in range(2):
                    yield ModelToolCall(
                        "one", "set_memory", {"key": "style", "value": "value"}
                    )
            else:
                yield ModelToolCall(
                    "one", "set_memory", {"key": "style", "value": "value"}
                )
        finally:
            closed.append(True)

    expected = {
        "revoke": PermissionError,
        "protected": MemoryContentBlocked,
        "timeout": TimeoutError,
    }.get(mode, ValueError)
    with pytest.raises(expected):
        await extract_memory_proposals(
            lease, check_grant=lambda: None, stream_tools=stream
        )
    assert closed == [True]
    assert MemoryRepository(lease.database).list("alice") == []


def test_manual_review_and_protected_input_prevent_extraction(tmp_path):
    repo, generation, lease = setup(tmp_path, text="Bearer private-token")
    configure(repo)
    repo.save_setting(
        "alice",
        "webui.filters.config",
        {
            "memories": {
                "pii": {"action": "block", "starterPatterns": ["bearer_header"]}
            }
        },
    )
    assert plan_extraction(lease) is None
    repo.save_setting("alice", "webui.filters.config", {})
    assert plan_extraction(lease) is not None
    with lease.database.connect() as db:
        state = json.loads(
            db.execute(
                "SELECT state FROM runs WHERE id=?", (generation.run_id,)
            ).fetchone()[0]
        )
        state["memoryManualReview"] = True
        db.execute(
            "UPDATE runs SET state=? WHERE id=?",
            (json.dumps(state), generation.run_id),
        )
    assert plan_extraction(lease) is None


@pytest.mark.asyncio
async def test_apply_memory_batch_once_without_disclosing_values(tmp_path):
    from workgate.hosted_chat.memory_application import apply_memory_proposals

    repo, generation, lease = setup(
        tmp_path, agent_config={"memory_scope": "agent"}
    )
    aid = generation.agent_id
    assert aid is not None
    memory = MemoryRepository(lease.database)
    memory.write("alice", "old", "Remove old", agent_id=aid)
    memory.write("alice", "old", "Personal stays")
    configure(repo, validKeys=["style", "old"])

    async def stream(*args, **kwargs):
        yield ModelToolCall(
            "one", "set_memory", {"key": "style", "value": "Private value."}
        )
        yield ModelToolCall("two", "delete_memory", {"key": "old"})

    proposals = await extract_memory_proposals(
        lease, check_grant=lambda: None, stream_tools=stream
    )
    assert proposals is not None
    assert apply_memory_proposals(
        lease, proposals, expires=None, check_grant=lambda: None
    ) == {
        "updated": 1,
        "deleted": 1,
    }
    assert [entry["key"] for entry in memory.list("alice", aid)] == ["style"]
    assert memory.list("alice", "")[0]["value"] == "Personal stays"
    assert plan_extraction(lease) is None
    events = GenerationStore(lease.database).events("alice", generation.run_id)
    assert events[-1][1]["event"] == "on_memory_update"
    assert "Private value." not in repr(events)
    memory.write("alice", "style", "Manual change.", agent_id=aid)
    assert apply_memory_proposals(
        lease, proposals, expires=None, check_grant=lambda: None
    ) == {
        "updated": 1,
        "deleted": 1,
    }
    assert memory.list("alice", aid)[0]["value"] == "Manual change."
    assert (
        GenerationStore(lease.database).events("alice", generation.run_id)
        == events
    )
    from dataclasses import replace

    with pytest.raises(PermissionError, match="already settled"):
        apply_memory_proposals(
            lease,
            replace(proposals, calls=()),
            expires=None,
            check_grant=lambda: None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["quota", "optout", "stale", "channel", "expired", "duplicate", "too_many"],
)
async def test_apply_memory_atomic_denials(tmp_path, mode):
    from dataclasses import replace

    from workgate.hosted_chat.credential_cipher import CredentialCipher
    from workgate.hosted_chat.memory_application import apply_memory_proposals
    from workgate.hosted_chat.provider_profiles import ProviderRepository

    repo, generation, lease = setup(tmp_path)
    memory = MemoryRepository(lease.database)
    configure(
        repo,
        validKeys=["style", "other"],
        charLimit=10 if mode == "quota" else 10000,
    )

    async def stream(*args, **kwargs):
        yield ModelToolCall(
            "one", "set_memory", {"key": "style", "value": "Short."}
        )
        yield ModelToolCall(
            "two",
            "set_memory",
            {
                "key": "style" if mode == "duplicate" else "other",
                "value": "A longer value.",
            },
        )

    proposals = await extract_memory_proposals(
        lease, check_grant=lambda: None, stream_tools=stream
    )
    assert proposals is not None
    if mode == "optout":
        repo.save_setting("alice", "user.memories", False)
    elif mode == "stale":
        memory.write("alice", "other", "Manual update.")
    elif mode == "channel":
        ProviderRepository(
            lease.database, CredentialCipher(lease.database)
        ).save(
            {
                "name": "test-provider",
                "provider": "openai",
                "models": ["test-model"],
                "base_url": "https://different.invalid/v1",
                "api_key": "new-key",
            }
        )
    elif mode == "too_many":
        proposals = replace(
            proposals,
            calls=tuple(
                ModelToolCall(str(i), "delete_memory", {"key": "style"})
                for i in range(9)
            ),
        )
    before = memory.list("alice")
    events = GenerationStore(lease.database).events("alice", generation.run_id)
    expected = (
        ValueError
        if mode in {"quota", "duplicate", "too_many"}
        else PermissionError
    )
    with pytest.raises(expected):
        apply_memory_proposals(
            lease,
            proposals,
            expires=1 if mode == "expired" else None,
            check_grant=lambda: None,
        )
    assert memory.list("alice") == before
    assert (
        GenerationStore(lease.database).events("alice", generation.run_id)
        == events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["events", "grant"])
async def test_memory_apply_rolls_back_on_late_failure(
    tmp_path, monkeypatch, fail_at
):
    from workgate.hosted_chat import memory_application

    repo, generation, lease = setup(tmp_path)
    configure(repo)

    async def stream(*args, **kwargs):
        yield ModelToolCall(
            "one", "set_memory", {"key": "style", "value": "Short."}
        )

    proposals = await extract_memory_proposals(
        lease, check_grant=lambda: None, stream_tools=stream
    )
    assert proposals is not None
    checked = 0

    def check():
        nonlocal checked
        checked += 1
        if fail_at == "grant" and checked == 3:
            raise PermissionError("Revoked at commit")

    if fail_at == "events":
        monkeypatch.setattr(memory_application, "EVENT_LIMIT", 0)
    with pytest.raises(ValueError if fail_at == "events" else PermissionError):
        memory_application.apply_memory_proposals(
            lease, proposals, expires=None, check_grant=check
        )
    assert MemoryRepository(lease.database).list("alice") == []
    assert not any(
        event.get("event") == "on_memory_update"
        for _, event in GenerationStore(lease.database).events(
            "alice", generation.run_id
        )
    )
    assert plan_extraction(lease) is not None
