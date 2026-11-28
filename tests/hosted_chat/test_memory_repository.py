"""Scoped memory transactions, quotas and current-policy projection."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.memory import (
    MemoryLimitExceeded,
    MemoryRepository,
    agent_partition,
    memory_config,
    memory_enabled,
)
from workgate.hosted_chat.memory_protection import MemoryContentBlocked
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.hosted_chat.tokenizer import count_tokens


def test_scoped_memory_revision_and_recovery(tmp_path):
    database = SQLiteDatabase(tmp_path)
    store = MemoryRepository(database)
    agents = AgentRepository(database)
    agent = agents.create(
        "alice",
        {"provider": "openai", "model": "test", "memory_scope": "agent"},
    )
    aid = agent_partition(agent)
    first = store.write("alice", "style", "Concise answers.")
    scoped = store.write("alice", "style", "Detailed answers.", agent_id=aid)
    assert first["_id"] != scoped["_id"]
    assert store.list("alice", "") == [first]
    assert store.list("alice", aid) == [scoped]
    assert store.list("bob") == []
    with pytest.raises(KeyError):
        store.write("bob", "style", "Wrong owner.", agent_id=aid)
    with pytest.raises(KeyError):
        store.list("bob", aid)
    with pytest.raises(KeyError):
        store.write("alice", "style", "Wrong ID.", existing_id=scoped["_id"])
    assert not store.delete("bob", entry_id=scoped["_id"], agent_id=aid)
    store.write("alice", "other", "Another memory.")
    with pytest.raises(FileExistsError):
        store.write("alice", "other", "Collision.", existing_id=first["_id"])
    revised = store.write(
        "alice", "renamed", "Revised.", existing_id=first["_id"]
    )
    assert revised["_id"] == first["_id"]
    assert (
        MemoryRepository(SQLiteDatabase(tmp_path))
        .formatted("alice")["withKeys"]
        .endswith('["value": "Revised."]')
    )
    agents.delete("alice", aid)
    with pytest.raises(KeyError):
        store.list("alice", aid)
    assert store.delete("alice", entry_id=scoped["_id"], agent_id=aid)
    assert store.delete_all("alice") == 2


def test_live_policy_and_configured_quota(tmp_path):
    database = SQLiteDatabase(tmp_path)
    store = MemoryRepository(database)
    repo = WorkspaceRepository(database)
    entry = store.write("alice", "secret", "Bearer private-token")
    repo.save_setting(
        "alice",
        "webui.filters.config",
        {
            "memories": {
                "pii": {"action": "block", "starterPatterns": ["bearer_header"]}
            }
        },
    )
    assert store.list("alice")[0]["value"] == ""
    assert store.list("alice")[0]["contentFilterBlocked"]
    assert "private-token" not in str(store.formatted("alice"))
    assert store.formatted("alice")["tokenCountsByKey"] == {}
    assert store.formatted("alice")["totalTokens"] == entry["tokenCount"]
    with pytest.raises(MemoryContentBlocked):
        store.write("alice", "new", "Bearer private-token")
    assert store.delete("alice", entry_id=entry["_id"])
    repo.save_setting(
        "alice",
        "webui.memory.config",
        {"charLimit": 10, "tokenLimit": count_tokens("short", exact=False)},
    )
    store.write("alice", "first", "short")
    with pytest.raises(MemoryLimitExceeded):
        store.write("alice", "second", "short", token_limit=100)
    with pytest.raises(ValueError):
        store.write("alice", "first", "a" * 11)
    repo.save_setting("alice", "user.memories", False)
    assert not memory_enabled(repo, "alice")
    assert store.write("alice", "first", "short")
    repo.save_setting("alice", "webui.memory.config", {"disabled": True})
    assert memory_config(repo, "alice") is None
    with pytest.raises(PermissionError):
        store.write("alice", "first", "short")


def test_concurrent_memory_quota_serialized(tmp_path):
    store = MemoryRepository(SQLiteDatabase(tmp_path))
    token_limit = count_tokens("one memory", exact=False) * 2

    def write(index):
        try:
            store.write(
                "alice",
                "key_" + chr(97 + index),
                "one memory",
                token_limit=token_limit,
                create=True,
            )
            return True
        except MemoryLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(8)))
    assert sum(results) == 2
    assert store.formatted("alice")["totalTokens"] <= token_limit


def test_memory_capabilities_and_quota_diagnostics(tmp_path, monkeypatch):
    import json
    import sqlite3

    from workgate.hosted_chat.memory import (
        format_memory_context,
        tool_definitions,
    )

    database = SQLiteDatabase(tmp_path)
    repo = WorkspaceRepository(database)
    store = MemoryRepository(database)
    assert memory_enabled(repo, "alice", tools=True)
    repo.save_setting("alice", "endpoints", {"agents": {"capabilities": []}})
    assert memory_enabled(repo, "alice")
    assert not memory_enabled(repo, "alice", tools=True)
    repo.save_setting(
        "alice",
        "endpoints",
        {"agents": {"capabilities": ["memory"]}, "memory": {"charLimit": 100}},
    )
    assert memory_config(repo, "alice") == {"charLimit": 100}
    assert memory_enabled(repo, "alice", tools=True)
    assert agent_partition(None) == ""
    assert agent_partition({"memory_scope": "agent", "id": 7}) == ""
    assert (
        agent_partition({"memory_scope": "agent", "id": "agent_a____1"})
        == "agent_a"
    )
    assert format_memory_context(None) is None
    assert format_memory_context("")
    assert (format_memory_context("known") or "").endswith("known")
    assert [item["function"]["name"] for item in tool_definitions()] == [
        "set_memory",
        "delete_memory",
    ]
    assert (
        "Must be one of: style"
        in tool_definitions(["style"])[0]["function"]["parameters"][
            "properties"
        ]["key"]["description"]
    )
    first = store.write("alice", "first", "Short.")
    with pytest.raises(MemoryLimitExceeded) as rejected:
        store.write(
            "alice",
            "second",
            "Longer phrase.",
            token_limit=first["tokenCount"] + 1,
        )
    artifact = rejected.value.artifact()["memory"]
    assert artifact["type"] == "error"
    assert json.loads(artifact["value"])["errorType"] == "would_exceed"
    with pytest.raises(ValueError):
        store.delete("alice")
    assert store.delete("alice", key="first")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("private database error")

    monkeypatch.setattr(store, "list", broken)
    failed = store.formatted("alice")
    assert failed["readFailed"] and failed["withKeys"] is None
    assert "private database error" not in repr(failed)


@pytest.mark.parametrize(
    "key,value,kwargs",
    [
        ("Invalid", "value", {}),
        ("nothing", "value", {}),
        ("valid", "", {}),
        ("valid", "value", {"char_limit": 0}),
        ("valid", "value", {"token_limit": -1}),
    ],
)
def test_memory_invalid_input(tmp_path, key, value, kwargs):
    store = MemoryRepository(SQLiteDatabase(tmp_path))
    with pytest.raises(ValueError):
        store.write("alice", key, value, **kwargs)
