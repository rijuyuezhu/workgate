"""Pinned approval precedence and native risk mapping on real owned storage."""

import pytest

from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.approval_settings import (
    GLOBAL_KEY,
    call_decision,
    effective_mode,
    save_mode,
    validate_mode,
)
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.storage import WorkspaceRepository


def test_approval_modes_precedence_owner_and_agent_versions(tmp_path):
    repo = WorkspaceRepository(SQLiteDatabase(tmp_path))
    chat = repo.create_conversation("alice", {"title": "chat"})[
        "conversationId"
    ]
    other = repo.create_conversation("bob", {"title": "chat"})["conversationId"]
    assert effective_mode(repo, "alice", chat) == "automatic"
    assert save_mode(repo, "alice", "dangerous") == "dangerous"
    assert effective_mode(repo, "alice", chat) == "dangerous"
    assert effective_mode(repo, "bob", other) == "automatic"
    agents = AgentRepository(repo.database)
    agent = agents.create(
        "alice", {"provider": "openai", "model": "test", "approval_mode": "all"}
    )
    assert effective_mode(repo, "alice", chat, agent) == "all"
    updated = agents.update(
        "alice", agent["id"], {"approval_mode": "automatic"}
    )
    assert updated is not None
    assert effective_mode(repo, "alice", chat, updated) == "automatic"
    restored = agents.revert("alice", agent["id"], 1)
    assert restored is not None and restored["approval_mode"] == "all"
    assert (
        save_mode(repo, "alice", "automatic", conversation_id=chat)
        == "automatic"
    )
    assert effective_mode(repo, "alice", chat, restored) == "automatic"
    assert (
        effective_mode(repo, "alice", chat, restored, parent_mode="all")
        == "all"
    )
    assert (
        save_mode(repo, "alice", "inherit", conversation_id=chat) == "inherit"
    )
    assert effective_mode(repo, "alice", chat, restored) == "all"
    with pytest.raises(KeyError, match="Conversation"):
        save_mode(repo, "bob", "all", conversation_id=chat)
    with pytest.raises(KeyError, match="Conversation"):
        effective_mode(repo, "bob", chat)
    assert repo.setting("alice", GLOBAL_KEY) == "dangerous"
    for bad in (None, True, "inherit", [], {}):
        with pytest.raises(ValueError, match="Invalid approval"):
            save_mode(repo, "alice", bad)
    with pytest.raises(ValueError, match="Invalid approval"):
        agents.update("alice", agent["id"], {"approval_mode": "bad"})
    assert validate_mode("inherit", inherit=True) == "inherit"


@pytest.mark.parametrize(
    "name,args,review",
    [
        ("read", {}, False),
        ("list_files", {}, False),
        ("read_persistent_shell_output", {}, False),
        ("write_file", {}, True),
        ("bash", {"command": "ls"}, False),
        ("bash", {"command": "pwd"}, False),
        ("bash", {"command": "ls > out"}, True),
        ("bash", {"command": ["ls"]}, True),
        ("bash", {"command": "ls", "async_": True}, True),
        ("bash", {"command": "ls", "pty": True}, True),
        ("bash", {"command": "ls", "persist_on_timeout": True}, True),
        ("bash", {"command": "ls", "env": {"PATH": "custom"}}, True),
        ("run_python_code", {}, True),
        ("send_persistent_shell_input", {}, True),
        ("web_search", {}, False),
        ("web_fetch", {}, False),
        ("browser_session", {"action": "list"}, False),
        ("browser_session", {"action": ["list"]}, True),
        ("session_start", {}, False),
        ("session_end", {}, True),
        ("session_copy", {}, True),
        ("session_change_workdir", {}, True),
        ("task", {"action": "create"}, False),
        ("task", {"action": "report"}, False),
        ("task", {"action": "finish"}, False),
        ("task", {"action": "delete"}, True),
        ("task_plan", {}, False),
        ("write_todos", {}, False),
        ("job", {"list_jobs": True}, False),
        ("job", {"poll": ["job"]}, False),
        ("job", {"cancel": ["job"]}, True),
        ("job", {"retry": ["job"]}, True),
        ("subagent", {}, False),
        ("unknown_mcp", {}, True),
    ],
)
def test_native_risk_policy(name, args, review):
    assert call_decision("automatic", None, name, args) is None
    assert call_decision("dangerous", None, name, args) == (
        {"decision": "ask"} if review else None
    )
    assert call_decision("all", None, name, args) == {"decision": "ask"}


def test_question_and_legacy_policy_remain_independent():
    assert call_decision("all", None, "ask_user_question", {}) is None
    policy = {
        "enabled": True,
        "mode": "bypass",
        "deny": ["write_file"],
        "ask": ["bash"],
    }
    assert call_decision("automatic", policy, "write_file", {}) == {
        "decision": "deny"
    }
    assert call_decision("automatic", policy, "bash", {}) == {"decision": "ask"}
    assert call_decision("all", policy, "read", {}) == {"decision": "ask"}
    assert call_decision(
        "all",
        {"enabled": True, "ask": ["ask_user_question"]},
        "ask_user_question",
        {},
    ) == {"decision": "ask"}
