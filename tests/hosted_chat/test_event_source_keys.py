"""Source-key authority is not a Controller or machine credential."""

import hashlib
import json

import pytest

from workgate.hosted_chat import event_source_keys as keys
from workgate.hosted_chat.database import SQLiteDatabase


def test_source_key_lifecycle_and_pending_revocation(tmp_path):
    db = SQLiteDatabase(tmp_path)
    repo = keys.EventSourceKeys(db)
    first = repo.mint(
        "alice",
        label="Webhook",
        permissions=["fire", "fire"],
        expires_at=keys._now() + 60_000,
    )
    second = repo.mint("bob")
    assert first["permissions"] == ["fire"]
    token = first["token"]
    assert repo.authenticate(token) == {
        "id": first["id"],
        "owner": "alice",
        "permissions": ["fire"],
    }
    authenticated = repo.authenticate(second["token"])
    assert authenticated is not None and authenticated["owner"] == "bob"
    assert repo.authenticate(token + "x") is None
    assert repo.authenticate(None) is None
    assert repo.authenticate("lsm_evt_" + "x" * 64) is None
    assert token not in json.dumps(repo.list("alice"))
    assert len(repo.list("alice")) == 1 and len(repo.list("bob")) == 1
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT token_hash FROM agent_event_source_keys WHERE id=?",
                (first["id"],),
            ).fetchone()[0]
            == hashlib.sha256(token.encode()).hexdigest()
        )
        assert keys.live_source(conn, "alice", first["id"], mode="fire")
        assert not keys.live_source(conn, "alice", first["id"], mode="steer")
        assert not keys.live_source(conn, "bob", first["id"])
        conn.execute(
            "INSERT INTO agent_event_deliveries(id,owner,source_key_id,fingerprint,envelope,mode,ordering_key,status,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
            (
                "delivery",
                "alice",
                first["id"],
                "fingerprint",
                "{}",
                "fire",
                "lane",
                1,
                1,
                1,
            ),
        )
    assert not repo.revoke("bob", first["id"])
    assert not repo.revoke("alice", "bad")
    assert repo.revoke("alice", first["id"])
    assert not repo.revoke("alice", first["id"])
    assert repo.authenticate(token) is None
    assert repo.list("alice")[0]["revokedAt"] is not None
    with db.read_snapshot() as conn:
        assert not keys.live_source(conn, "alice", first["id"])
        row = conn.execute(
            "SELECT status,error_code FROM agent_event_deliveries WHERE id='delivery'"
        ).fetchone()
        assert tuple(row) == ("dead", "SOURCE_REVOKED")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"label": ""},
        {"label": None},
        {"permissions": []},
        {"permissions": ["native"]},
        {"permissions": [{}]},
        {"expires_at": True},
        {"expires_at": 1},
        {"expires_at": 2**53},
    ],
)
def test_invalid_keys(tmp_path, kwargs):
    repo = keys.EventSourceKeys(SQLiteDatabase(tmp_path))
    with pytest.raises(ValueError):
        repo.mint("alice", **kwargs)


def test_key_expiry_and_quota(tmp_path, monkeypatch):
    db = SQLiteDatabase(tmp_path)
    repo = keys.EventSourceKeys(db)
    monkeypatch.setattr(keys, "_MAX_KEYS", 1)
    monkeypatch.setattr(keys, "_now", lambda: 100)
    first = repo.mint("alice", expires_at=101)
    assert repo.authenticate(first["token"])
    with pytest.raises(ValueError, match="limit"):
        repo.mint("alice")
    monkeypatch.setattr(keys, "_now", lambda: 101)
    assert repo.authenticate(first["token"]) is None
    with db.read_snapshot() as conn:
        assert not keys.live_source(conn, "alice", first["id"])
