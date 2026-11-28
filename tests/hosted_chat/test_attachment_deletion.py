import json
from pathlib import Path

import pytest

from workgate.hosted_chat.attachment_deletion import AttachmentDeletionStore
from workgate.hosted_chat.attachment_uploads import publish_attachment
from workgate.hosted_chat.database import SQLiteDatabase


def grant():
    pass


async def make_file(database, owner="alice"):
    async def chunks():
        yield b"abc"

    result = await publish_attachment(
        database,
        owner,
        "test.txt",
        "text/plain",
        chunks(),
        expected_size=3,
        check_grant=grant,
    )
    with database.connect() as db:
        path = Path(
            db.execute(
                "SELECT path FROM attachments WHERE id=?", (result["file_id"],)
            ).fetchone()["path"]
        )
    return result["file_id"], path


@pytest.mark.asyncio
async def test_delete_and_idempotent_retry(tmp_path):
    database = SQLiteDatabase(tmp_path)
    ident, path = await make_file(database)
    store = AttachmentDeletionStore(database)
    assert store.delete(
        "alice", [ident, ident, "absent"], check_grant=grant
    ) == {"deletedFileIds": [ident], "failedFileIds": []}
    assert not path.exists()
    assert not list(path.parent.glob(".delete-*"))
    assert store.delete("alice", [ident], check_grant=grant) == {
        "deletedFileIds": [],
        "failedFileIds": [],
    }
    with database.connect() as db:
        assert not db.execute("SELECT * FROM attachments").fetchall()
        assert not db.execute("SELECT * FROM attachment_deletions").fetchall()


@pytest.mark.asyncio
async def test_foreign_batch_rejected_before_any_deletion(tmp_path):
    database = SQLiteDatabase(tmp_path)
    own, path = await make_file(database)
    foreign, other = await make_file(database, "bob")
    with pytest.raises(PermissionError):
        AttachmentDeletionStore(database).delete(
            "alice", [own, foreign], check_grant=grant
        )
    assert path.read_bytes() == other.read_bytes() == b"abc"


@pytest.mark.asyncio
async def test_metadata_failure_restores_bytes(tmp_path):
    database = SQLiteDatabase(tmp_path)
    ident, path = await make_file(database)
    with database.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_delete BEFORE DELETE ON attachments BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    assert AttachmentDeletionStore(database).delete(
        "alice", [ident], check_grant=grant
    ) == {"deletedFileIds": [], "failedFileIds": [ident]}
    assert path.read_bytes() == b"abc"
    assert not list(path.parent.glob(".delete-*"))
    with database.connect() as db:
        assert db.execute(
            "SELECT id FROM attachments WHERE id=?", (ident,)
        ).fetchone()
        assert not db.execute("SELECT * FROM attachment_deletions").fetchall()


@pytest.mark.asyncio
async def test_revocation_before_commit(tmp_path):
    database = SQLiteDatabase(tmp_path)
    ident, path = await make_file(database)
    calls = 0

    def revoke():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError, match="revoked"):
        AttachmentDeletionStore(database).delete(
            "alice", [ident], check_grant=revoke
        )
    assert path.read_bytes() == b"abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase, collision, both_links",
    [
        ("prepared", False, False),
        ("prepared", False, True),
        ("prepared", True, False),
        ("committed", False, False),
    ],
)
async def test_recover_crash_windows(tmp_path, phase, collision, both_links):
    import os

    database = SQLiteDatabase(tmp_path)
    ident, path = await make_file(database)
    quarantine = path.with_name(".delete-test")
    os.link(path, quarantine)
    if not both_links:
        path.unlink()
    if collision:
        path.write_bytes(b"replacement")
    with database.connect() as db:
        db.execute(
            "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
            (ident, "alice", str(path), str(quarantine), phase),
        )
        if phase == "committed":
            db.execute("DELETE FROM attachments WHERE id=?", (ident,))
    result = AttachmentDeletionStore(database).recover()
    if collision:
        assert result["failedFileIds"] == [ident]
        assert path.read_bytes() == b"replacement"
        assert quarantine.read_bytes() == b"abc"
    else:
        assert result["recoveredFileIds"] == [ident]
        assert not quarantine.exists()
        if phase == "prepared":
            assert path.read_bytes() == b"abc"
        else:
            assert not path.exists()


@pytest.mark.asyncio
async def test_legacy_agent_reference_not_orphaned(tmp_path):
    from workgate.hosted_chat.agents import AgentRepository

    database = SQLiteDatabase(tmp_path)
    ident, path = await make_file(database)
    agent = AgentRepository(database).create(
        "alice", {"name": "test", "provider": "openai", "model": "test-model"}
    )
    with database.connect() as db:
        row = db.execute(
            "SELECT config FROM agents WHERE id=?", (agent["id"],)
        ).fetchone()
        config = json.loads(row["config"])
        config["tool_resources"] = {"context": {"file_ids": [ident]}}
        db.execute(
            "UPDATE agents SET config=? WHERE id=?",
            (json.dumps(config), agent["id"]),
        )
    assert AttachmentDeletionStore(database).delete(
        "alice", [ident], check_grant=grant
    )["failedFileIds"] == [ident]
    assert path.read_bytes() == b"abc"


@pytest.mark.asyncio
async def test_invalid_or_unsafe_path_never_touched(tmp_path):
    database = SQLiteDatabase(tmp_path)
    ident, path = await make_file(database)
    outside = tmp_path / "outside"
    outside.write_bytes(b"other")
    store = AttachmentDeletionStore(database)
    with database.connect() as db:
        db.execute(
            "UPDATE attachments SET path=? WHERE id=?", (str(outside), ident)
        )
    assert store.delete("alice", [ident], check_grant=grant)[
        "failedFileIds"
    ] == [ident]
    assert outside.read_bytes() == b"other"
    assert path.read_bytes() == b"abc"
    with pytest.raises(ValueError):
        store.delete("alice", [""], check_grant=grant)
