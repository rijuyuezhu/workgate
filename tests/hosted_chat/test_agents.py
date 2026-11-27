"""LibreChat Agent management contract against the native SQLite store."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from workgate.hosted_chat.agent_validation import (
    clean_agent_config,
    sanitize_model_parameters,
)
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.provider_profiles import (
    ChannelConflict,
    ProviderRepository,
)
from workgate.hosted_chat.storage import WorkspaceRepository


def test_agent_create_update_revision_and_restore(tmp_path):
    repo = AgentRepository(SQLiteDatabase(tmp_path))
    created = repo.create(
        "alice",
        {
            "name": "Research",
            "provider": "openAI",
            "model": "gpt-4o-mini",
            "model_parameters": {"temperature": 0.5},
            "instructions": "Use tools",
            "tools": ["ask_user_question"],
        },
    )
    agent_id = created["id"]
    assert agent_id.startswith("agent_")
    assert created["version"] == 1
    assert created["tools"] == ["ask_user_question"]
    assert created["model_parameters"]["temperature"] == 0.5
    assert repo.get("bob", agent_id) is None
    assert repo.list("bob")["data"] == []
    updated = repo.update(
        "alice",
        agent_id,
        {"name": "Updated", "instructions": "New instructions"},
    )
    assert updated is not None
    assert updated["version"] == 2
    current = repo.get("alice", agent_id)
    assert current is not None and current["instructions"] == "New instructions"
    history = repo.versions("alice", agent_id)
    assert history is not None
    assert len(history) == 2
    assert history[-1]["instructions"] == "Use tools"
    restored = repo.revert("alice", agent_id, 1)
    assert restored is not None
    assert restored["instructions"] == "Use tools"
    assert restored["version"] == 3
    clone = repo.duplicate("alice", agent_id)
    assert clone is not None
    assert clone["agent"]["id"] != agent_id
    assert clone["agent"]["name"].endswith("(copy)")
    assert clone["actions"] == []
    assert not repo.delete("bob", agent_id)
    assert repo.delete("alice", agent_id)
    assert repo.versions("alice", agent_id) is None
    with repo.database.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM agent_versions WHERE agent_id=?",
                (agent_id,),
            ).fetchone()[0]
            == 0
        )


def test_agent_query_cursor_and_allowlist(tmp_path):
    repo = AgentRepository(SQLiteDatabase(tmp_path))
    for i in range(5):
        repo.create(
            "local",
            {
                "name": f"NJU Agent {i}",
                "category": "research",
                "provider": "openAI",
                "model": None,
                "api_key": "secret-should-never-be-persisted",
                "password": "secret",
            },
        )
    seen = []
    cursor = None
    while True:
        page = repo.list("local", limit=2, cursor=cursor, category="research")
        seen.extend(item["id"] for item in page["data"])
        assert all(
            "api_key" not in item and "password" not in item
            for item in page["data"]
        )
        if not page["has_more"]:
            break
        cursor = page["after"]
    assert len(set(seen)) == 5
    assert len(repo.list("local", search="NJU")["data"]) == 5
    assert repo.list("local", category="tools")["data"] == []
    with pytest.raises(ValueError):
        repo.create("local", {"name": "Missing provider"})
    with pytest.raises(ValueError):
        repo.create(
            "local",
            {
                "name": "Invalid tools",
                "provider": "openAI",
                "tools": "not list",
            },
        )


def test_exact_restore_and_failed_revision_are_atomic(tmp_path):
    repo = AgentRepository(SQLiteDatabase(tmp_path))
    created = repo.create("alice", {"provider": "模型一", "model": None})
    aid = created["id"]
    updated = repo.update(
        "alice",
        aid,
        {
            "instructions": "new",
            "model_parameters": {
                "temperature": "0",
                "maxTokens": "placeholder",
            },
        },
    )
    assert updated is not None and updated["model_parameters"] == {
        "temperature": 0
    }
    with pytest.raises(ValueError):
        repo.update("alice", aid, {"tools": ["read", {}]})
    with pytest.raises(ValueError):
        repo.update(
            "alice", aid, {"model_parameters": {"custom": float("nan")}}
        )
    assert repo.get("alice", aid) == updated
    for invalid in (-1, True, 2, 2**64):
        with pytest.raises(ValueError, match="Unknown agent version"):
            repo.revert("alice", aid, invalid)
    assert repo.get("alice", aid) == updated
    restored = repo.revert("alice", aid, 1)
    assert restored is not None and restored["version"] == 3
    assert "instructions" not in restored
    assert restored["model_parameters"] == {}
    assert restored["provider"] == "模型一"
    history = repo.versions("alice", aid)
    assert history is not None and [v["version"] for v in history] == [3, 2, 1]
    assert (
        history[1]["instructions"] == "new" and "instructions" not in history[0]
    )
    assert repo.update("bob", aid, {"name": "tamper"}) is None
    assert repo.duplicate("bob", aid) is None
    assert repo.revert("bob", aid, 0) is None
    assert repo.versions("bob", aid) is None
    assert repo.get("alice", aid) == restored


def test_owner_reference_and_named_channel_history(tmp_path):
    database = SQLiteDatabase(tmp_path)
    profiles = ProviderRepository(database, CredentialCipher(database))
    profiles.save(
        {
            "name": "模型一",
            "provider": "openai",
            "base_url": "https://models.invalid/v1",
            "models": ["a"],
            "api_key": "secret",
        }
    )
    repo = AgentRepository(database)
    agent = repo.create(
        "alice", {"provider": "模型一", "model": "a", "api_key": "never-store"}
    )
    aid = agent["id"]
    workspace = WorkspaceRepository(database)
    convo = workspace.create_conversation("alice", {"agent_id": aid})
    with pytest.raises(KeyError):
        workspace.create_conversation("bob", {"agent_id": aid})
    assert repo.update("alice", aid, {"provider": "openai"}) is not None
    with pytest.raises(ChannelConflict):
        profiles.delete(name="模型一")
    with database.connect() as db:
        rows = db.execute(
            "SELECT snapshot FROM agent_versions WHERE agent_id=?", (aid,)
        ).fetchall()
    assert all(
        "never-store" not in row[0] and "api_key" not in json.loads(row[0])
        for row in rows
    )
    assert repo.delete("alice", aid)
    assert workspace.conversation("alice", convo["conversationId"]) == convo
    profiles.delete(name="模型一")


def test_literal_search_and_concurrent_revisions(tmp_path):
    repo = AgentRepository(SQLiteDatabase(tmp_path))
    aid = repo.create(
        "alice", {"provider": "openai", "model": None, "name": "a%_b"}
    )["id"]
    repo.create("alice", {"provider": "openai", "model": None, "name": "axxb"})
    assert [a["id"] for a in repo.list("alice", search="%_")["data"]] == [aid]
    with pytest.raises(ValueError, match="Invalid pagination cursor"):
        repo.list("alice", cursor="not-a-cursor")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda i: repo.update("alice", aid, {"instructions": str(i)}),
                range(8),
            )
        )
    versions = [r["version"] for r in results if r is not None]
    assert sorted(versions) == list(range(2, 10))
    history = repo.versions("alice", aid)
    assert history is not None and [v["version"] for v in history] == list(
        range(9, 0, -1)
    )


def test_numeric_parameters_ignore_nonfinite_and_placeholders():
    assert sanitize_model_parameters(
        {
            "temperature": "0",
            "topP": "0x10",
            "maxTokens": "Infinity",
            "thinkingBudget": True,
            "max_tokens": 10**400,
            "custom": {"x": 1},
        }
    ) == {"temperature": 0, "topP": 16, "custom": {"x": 1}}


@pytest.mark.parametrize(
    "fixture",
    ["agent_parameter_vectors.json", "agent_core_validation_vectors.json"],
)
def test_pinned_authoring_oracle(fixture):
    vectors = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "hosted_chat"
            / fixture
        ).read_text(encoding="utf-8")
    )
    for case, expected in zip(
        vectors["cases"], vectors["expected"], strict=True
    ):
        if fixture == "agent_parameter_vectors.json":
            assert sanitize_model_parameters(case) == expected
            continue
        try:
            actual = {
                "ok": True,
                "value": clean_agent_config(
                    case["payload"], create=case.get("create", False)
                ),
            }
        except ValueError:
            actual = {"ok": False}
        if case["payload"].get("provider") == "":
            # Workgate requires a usable named channel or built-in identity.
            assert actual == {"ok": False}
        else:
            assert actual == expected, case


def test_saved_graph_authoring_rebinds_self_and_preserves_revisions(tmp_path):
    from workgate.hosted_chat.graph_topology import admit_agent_graph

    repo = AgentRepository(SQLiteDatabase(tmp_path))
    child = repo.create("alice", {"provider": "openai", "model": "child"})
    parent = repo.create(
        "alice",
        {
            "provider": "openai",
            "model": "parent",
            "edges": [
                {
                    "from": "",
                    "to": child["id"],
                    "handoffScope": "conversation",
                    "untrusted": "drop",
                }
            ],
            "maxHandoffs": 3,
        },
    )
    assert parent["edges"] == [
        {
            "from": parent["id"],
            "to": child["id"],
            "handoffScope": "conversation",
        }
    ]
    updated = repo.update(
        "alice",
        parent["id"],
        {
            "graphAgentIds": [parent["id"], child["id"]],
            "entryAgentId": parent["id"],
            "resultAgentId": parent["id"],
            "edges": [
                *parent["edges"],
                {"from": child["id"], "to": parent["id"]},
            ],
        },
    )
    assert updated is not None
    topology = admit_agent_graph("alice", updated, repo)
    assert topology is not None and topology.default_agent_id == parent["id"]
    duplicate = repo.duplicate("alice", parent["id"])
    assert duplicate is not None
    copy = duplicate["agent"]
    assert copy["entryAgentId"] == copy["resultAgentId"] == copy["id"]
    assert copy["graphAgentIds"] == [copy["id"], child["id"]]
    assert copy["edges"][0]["from"] == copy["id"]
    assert copy["edges"][1]["to"] == copy["id"]
    cloned = admit_agent_graph("alice", copy, repo)
    assert cloned is not None and cloned.default_agent_id == copy["id"]
    restored = repo.revert("alice", parent["id"], 1)
    assert restored is not None and restored["edges"] == parent["edges"]
    assert "entryAgentId" not in restored


def test_graph_references_fail_atomically_on_foreign_or_deleted_agents(
    tmp_path,
):
    repo = AgentRepository(SQLiteDatabase(tmp_path))
    owned = repo.create("alice", {"provider": "openai", "model": "child"})
    foreign = repo.create("bob", {"provider": "openai", "model": "foreign"})
    parent = repo.create("alice", {"provider": "openai", "model": "parent"})
    before = repo.list("alice")
    with pytest.raises(PermissionError, match="not accessible"):
        repo.create(
            "alice",
            {
                "provider": "openai",
                "model": "bad",
                "edges": [{"from": "", "to": foreign["id"]}],
            },
        )
    assert repo.list("alice") == before
    with pytest.raises(ValueError, match="does not exist"):
        repo.update(
            "alice", parent["id"], {"edges": [{"from": "", "to": "missing"}]}
        )
    with pytest.raises(PermissionError, match="not accessible"):
        repo.update("alice", parent["id"], {"graphAgentIds": [foreign["id"]]})
    with pytest.raises(ValueError, match="unknown entryAgentId"):
        repo.update("alice", parent["id"], {"entryAgentId": owned["id"]})
    assert repo.get("alice", parent["id"]) == parent
    graph = repo.update(
        "alice",
        parent["id"],
        {"edges": [{"from": "", "to": owned["id"], "edgeType": "direct"}]},
    )
    assert graph is not None
    cleared = repo.update("alice", parent["id"], {"edges": []})
    assert cleared is not None
    assert repo.delete("alice", owned["id"])
    with pytest.raises(ValueError, match="does not exist"):
        repo.revert("alice", parent["id"], 1)
    assert repo.get("alice", parent["id"]) == cleared


@pytest.mark.parametrize(
    "changes",
    [
        {"edges": [{"from": [], "to": "a"}]},
        {"edges": [{"from": "", "to": ""}]},
        {"edges": [{"to": "a"}]},
        {"edges": [{"from": "", "to": "a", "edgeType": "invalid"}]},
        {"edges": [{"from": "", "to": "a", "handoffScope": "invalid"}]},
        {"edges": [{"from": "", "to": "a", "excludeResults": 1}]},
        {"maxHandoffs": True},
        {"maxHandoffs": -1},
        {"maxHandoffs": 2**53},
    ],
)
def test_graph_authoring_rejects_malformed_fields(tmp_path, changes):
    repo = AgentRepository(SQLiteDatabase(tmp_path))
    with pytest.raises(ValueError):
        repo.create("alice", {"provider": "openai", "model": "m", **changes})
    assert not repo.list("alice")["data"]
