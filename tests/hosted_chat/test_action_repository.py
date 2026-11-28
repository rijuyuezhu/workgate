"""Owner-bound Action records and shared encrypted credentials."""

import json

import pytest

from workgate.hosted_chat.action_spec import normalize_action_tool_name
from workgate.hosted_chat.actions import ActionRepository
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.credential_cipher import CredentialUnavailable
from workgate.hosted_chat.database import SQLiteDatabase


def body(
    domain="https://api.example.org",
    *,
    key: str | None = "test-action-secret",
    operation="ping",
):
    value = {
        "metadata": {
            "domain": domain,
            "raw_spec": json.dumps(
                {
                    "openapi": "3.0.3",
                    "servers": [{"url": domain + "/v1"}],
                    "paths": {
                        "/ping": {
                            "get": {"operationId": operation, "x-strict": True}
                        }
                    },
                }
            ),
            "auth": {"type": "service_http", "authorization_type": "bearer"},
        },
        "functions": [{"type": "function", "function": {"name": operation}}],
    }
    if key is not None:
        value["metadata"]["api_key"] = key
    return value


@pytest.fixture
def records(tmp_path):
    db = SQLiteDatabase(tmp_path)
    agents = AgentRepository(db)
    alice = agents.create(
        "alice", {"name": "Agent", "provider": "openAI", "model": None}
    )
    bob = agents.create(
        "bob", {"name": "Other", "provider": "openAI", "model": None}
    )
    return db, agents, ActionRepository(db), alice, bob


def test_storage_version_and_live_tool_binding(records):
    db, agents, repo, alice, bob = records
    updated, saved = repo.save("alice", alice["id"], body())
    ident = saved["action_id"]
    assert updated["version"] == 2 and saved["version"] == 1
    assert repo.list("alice") == [saved]
    assert repo.list("bob") == []
    assert "test-action-secret" not in json.dumps(saved)
    assert "api_key" not in json.dumps(saved)
    assert "test-action-secret" not in json.dumps(
        agents.versions("alice", alice["id"])
    )
    assert b"test-action-secret" not in db.path.read_bytes()
    name = normalize_action_tool_name(updated["tools"][0])
    assert repo.tool_schemas("bob", updated) == []
    assert repo.tool_schemas("alice", updated, selected_tools=[]) == []
    assert repo.tool_schemas("alice", updated, selected_tools=[name])[0][
        "function"
    ]["strict"]
    resolved = repo.get_for_tool("alice", alice["id"], name)
    assert resolved is not None
    metadata, credentials, op = resolved
    assert metadata == saved["metadata"]
    assert credentials == {"api_key": "test-action-secret"}
    assert op["action_id"] == ident and op["action_version"] == 1
    assert repo.get_for_tool("bob", bob["id"], name) is None
    assert repo.get_for_tool("alice", alice["id"], "unselected") is None
    with pytest.raises(KeyError):
        repo.delete("bob", alice["id"], ident)
    patch = body(key=None)
    patch["action_id"] = ident
    second, action = repo.save("alice", alice["id"], patch)
    assert second["version"] == 3 and action["version"] == 2
    resolved = repo.get_for_tool("alice", alice["id"], name)
    assert resolved is not None and resolved[1] == credentials
    changed = body("https://new.example.org", key=None)
    changed["action_id"] = ident
    with pytest.raises(ValueError, match="credentials must be re-entered"):
        repo.save("alice", alice["id"], changed)
    agents.update("alice", alice["id"], {"tools": []})
    assert repo.tool_schemas("alice", second) == []
    assert repo.get_for_tool("alice", alice["id"], name) is None
    assert repo.delete("alice", alice["id"], ident)
    assert repo.list("alice") == []
    assert not repo.delete("alice", alice["id"], ident)


def test_collision_and_cross_agent_updates_fail_atomically(records):
    _, agents, repo, alice, bob = records
    updated, saved = repo.save("alice", alice["id"], body())
    duplicate = body()
    with pytest.raises(ValueError, match="collides"):
        repo.save("alice", alice["id"], duplicate)
    duplicate["action_id"] = saved["action_id"]
    with pytest.raises(PermissionError):
        repo.save("bob", bob["id"], duplicate)
    assert agents.get("alice", alice["id"]) == updated
    assert repo.list("alice") == [saved]


def test_cipher_bound_to_action_and_version(records):
    db, _, repo, alice, _ = records
    a, first = repo.save("alice", alice["id"], body())
    _, second = repo.save("alice", alice["id"], body(operation="other"))
    with db.connect() as conn:
        sealed = conn.execute(
            "SELECT credentials FROM action_records WHERE id=?",
            (first["action_id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE action_records SET credentials=? WHERE id=?",
            (sealed, second["action_id"]),
        )
    with pytest.raises(CredentialUnavailable):
        repo.get_for_tool(
            "alice",
            alice["id"],
            normalize_action_tool_name(
                next(
                    t
                    for t in repo.agents.get("alice", alice["id"])["tools"]
                    if t.startswith("other_action_")
                )
            ),
        )
    patch = body(key=None)
    patch["action_id"] = first["action_id"]
    repo.save("alice", alice["id"], patch)
    with db.connect() as conn:
        conn.execute(
            "UPDATE action_records SET credentials=? WHERE id=?",
            (sealed, first["action_id"]),
        )
    with pytest.raises(CredentialUnavailable):
        repo.get_for_tool(
            "alice", alice["id"], normalize_action_tool_name(a["tools"][0])
        )


@pytest.mark.parametrize(
    "mode",
    [
        "empty",
        "metadata",
        "bad_id",
        "unknown",
        "auth_type",
        "auth_key",
        "auth_field",
        "custom_header",
        "oauth_missing",
        "oauth_url",
        "oauth_method",
        "oauth_scope",
        "key_missing",
        "mismatch",
        "long_name",
        "large_key",
    ],
)
def test_bad_import_is_atomic(records, mode):
    _, agents, repo, alice, _ = records
    value = body()
    meta = value["metadata"]
    if mode == "empty":
        value["functions"] = []
    elif mode == "metadata":
        value["metadata"] = None
    elif mode == "bad_id":
        value["action_id"] = "../bad"
    elif mode == "unknown":
        meta["unknown"] = "x"
    elif mode == "auth_type":
        meta["auth"] = {"type": "unknown"}
    elif mode == "auth_key":
        meta["auth"] = {"type": "service_http", "authorization_type": "invalid"}
    elif mode == "auth_field":
        meta["auth"]["unknown"] = "x"
    elif mode == "custom_header":
        meta["auth"] = {
            "type": "service_http",
            "authorization_type": "custom",
            "custom_auth_header": "Host",
        }
    elif mode.startswith("oauth"):
        meta["auth"] = {
            "type": "oauth",
            "token_exchange_method": "default_post",
            "scope": "read",
            "authorization_url": "https://auth.example.org/auth",
            "client_url": "https://auth.example.org/token",
        }
        if mode == "oauth_url":
            meta["auth"]["client_url"] = "http://auth.example.org/token"
        elif mode == "oauth_method":
            meta["auth"]["token_exchange_method"] = "bad"
        elif mode == "oauth_scope":
            del meta["auth"]["scope"]
    elif mode == "key_missing":
        del meta["api_key"]
    elif mode == "mismatch":
        value["functions"][0]["function"]["name"] = "other"
    elif mode == "long_name":
        value = body(operation="x" * 60)
    elif mode == "large_key":
        meta["api_key"] = "x" * 16385
    with pytest.raises(ValueError):
        repo.save("alice", alice["id"], value)
    assert agents.get("alice", alice["id"]) == alice
    assert repo.list("alice") == []


def test_inactive_action_collision_and_auth_transition(records):
    db, agents, repo, alice, _ = records
    updated, saved = repo.save("alice", alice["id"], body())
    agents.update("alice", alice["id"], {"tools": []})
    with pytest.raises(ValueError, match="collides"):
        repo.save("alice", alice["id"], body())
    patch = body(key=None)
    patch["action_id"] = saved["action_id"]
    patch["metadata"]["auth"] = {"type": "none"}
    updated, saved = repo.save("alice", alice["id"], patch)
    result = repo.get_for_tool(
        "alice", alice["id"], normalize_action_tool_name(updated["tools"][0])
    )
    assert result is not None and result[1] == {}
    with db.read_snapshot() as conn:
        assert (
            conn.execute("SELECT credentials FROM action_records").fetchone()[0]
            is None
        )
