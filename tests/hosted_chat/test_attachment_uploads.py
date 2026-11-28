import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from workgate.hosted_chat.attachment_uploads import (
    MAX_ATTACHMENT_BYTES,
    publish_attachment,
    recover_attachment_uploads,
)
from workgate.hosted_chat.database import SQLiteDatabase


async def chunks(*values):
    for value in values:
        yield value


def grant():
    pass


@pytest.mark.asyncio
async def test_publish_private_bytes(tmp_path):
    database = SQLiteDatabase(tmp_path)
    result = await publish_attachment(
        database,
        "alice",
        r"C:\fake\name.txt",
        "text/plain",
        chunks(b"abc", b"def"),
        expected_size=6,
        check_grant=grant,
    )
    with database.connect() as db:
        row = db.execute("SELECT * FROM attachments").fetchone()
        assert row["owner"] == "alice"
        assert row["name"] == "name.txt"
        path = Path(row["path"])
        assert path.read_bytes() == b"abcdef"
        assert path.name == result["file_id"]
        assert (
            json.loads(row["metadata"])["sha256"]
            == hashlib.sha256(b"abcdef").hexdigest()
        )
        assert not db.execute("SELECT * FROM attachment_uploads").fetchall()
    assert "path" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("data,size", [(b"ab", 3), (b"abcd", 3), (b"", 3)])
async def test_invalid_stream_cleans_intent(tmp_path, data, size):
    database = SQLiteDatabase(tmp_path)
    with pytest.raises(ValueError):
        await publish_attachment(
            database,
            "alice",
            "test",
            "",
            chunks(data),
            expected_size=size,
            check_grant=grant,
        )
    with database.connect() as db:
        assert not db.execute("SELECT * FROM attachments").fetchall()
        assert not db.execute("SELECT * FROM attachment_uploads").fetchall()
    assert not list((database.root / "attachments").rglob("file_*"))


@pytest.mark.asyncio
async def test_revocation_and_cancellation_cleanup(tmp_path):
    database = SQLiteDatabase(tmp_path)
    calls = 0

    def revoke():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        await publish_attachment(
            database,
            "alice",
            "test",
            "",
            chunks(b"x"),
            expected_size=1,
            check_grant=revoke,
        )

    async def interrupted():
        yield b"x"
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await publish_attachment(
            database,
            "alice",
            "test",
            "",
            interrupted(),
            expected_size=2,
            check_grant=grant,
        )
    with database.connect() as db:
        assert not db.execute("SELECT * FROM attachments").fetchall()
        assert not db.execute("SELECT * FROM attachment_uploads").fetchall()


@pytest.mark.parametrize(
    "name,size",
    [("", 1), ("..", 1), ("a\n", 1), ("x", 0), ("x", MAX_ATTACHMENT_BYTES + 1)],
)
@pytest.mark.asyncio
async def test_validation(tmp_path, name, size):
    database = SQLiteDatabase(tmp_path)
    with pytest.raises(ValueError):
        await publish_attachment(
            database,
            "alice",
            name,
            "",
            chunks(b"x"),
            expected_size=size,
            check_grant=grant,
        )


def test_recovery_only_reclaims_verified_or_missing_files(tmp_path):
    database = SQLiteDatabase(tmp_path)
    root = database.root.resolve() / "attachments"
    root.mkdir()
    for name in ("verified", "changed", "unknown", "committed"):
        (root / name).write_bytes(b"abc")
    with database.connect() as db:
        for name in ("verified", "changed", "unknown", "missing", "committed"):
            path = root / name
            identity = None
            if path.exists() and name != "unknown":
                st = path.stat()
                identity = f"{st.st_dev}:{st.st_ino}"
            db.execute(
                "INSERT INTO attachment_uploads(file_id,owner,path,file_identity,content_size,content_sha256,created_at) VALUES(?,?,?,?,?,?,0)",
                (
                    name,
                    "alice",
                    str(path),
                    identity,
                    3,
                    hashlib.sha256(b"abc").hexdigest(),
                ),
            )
        db.execute(
            "INSERT INTO attachments(id,owner,name,path,content_type,metadata) VALUES(?,?,?,?,?,?)",
            (
                "committed",
                "alice",
                "test",
                str(root / "committed"),
                "text/plain",
                "{}",
            ),
        )
    (root / "changed").write_bytes(b"def")
    assert recover_attachment_uploads(database) == 3
    assert {p.name for p in root.iterdir()} == {
        "changed",
        "unknown",
        "committed",
    }
    with database.connect() as db:
        assert {
            r["file_id"]
            for r in db.execute("SELECT file_id FROM attachment_uploads")
        } == {"changed", "unknown"}
    assert recover_attachment_uploads(database) == 0


@pytest.mark.asyncio
async def test_linked_attachment_directory_rejected(tmp_path):
    database = SQLiteDatabase(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (database.root / "attachments").symlink_to(
            outside, target_is_directory=True
        )
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(OSError):
        await publish_attachment(
            database,
            "alice",
            "test",
            "",
            chunks(b"x"),
            expected_size=1,
            check_grant=grant,
        )
    assert not list(outside.iterdir())


@pytest.mark.asyncio
async def test_database_insert_failure_removes_verified_bytes(tmp_path):
    database = SQLiteDatabase(tmp_path)
    with database.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_publish BEFORE INSERT ON attachments BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await publish_attachment(
            database,
            "alice",
            "test",
            "",
            chunks(b"abc"),
            expected_size=3,
            check_grant=grant,
        )
    assert not list((database.root / "attachments").rglob("file_*"))
    with database.connect() as db:
        assert not db.execute("SELECT * FROM attachment_uploads").fetchall()


@pytest.mark.asyncio
async def test_recovery_during_stream_cannot_remove_live_bytes(tmp_path):
    database = SQLiteDatabase(tmp_path)

    async def streaming():
        yield b"a"
        assert recover_attachment_uploads(database, stale_seconds=0) == 0
        yield b"bc"

    await publish_attachment(
        database,
        "alice",
        "test",
        "text/plain",
        streaming(),
        expected_size=3,
        check_grant=grant,
    )
    with database.connect() as db:
        row = db.execute("SELECT path FROM attachments").fetchone()
        assert Path(row["path"]).read_bytes() == b"abc"


@pytest.mark.asyncio
async def test_changed_bytes_retained_for_manual_recovery(tmp_path):
    database = SQLiteDatabase(tmp_path)
    calls = 0

    def change():
        nonlocal calls
        calls += 1
        if calls == 3:
            path = next((database.root / "attachments").rglob("file_*"))
            path.write_bytes(b"xyz")

    with pytest.raises(OSError, match="changed"):
        await publish_attachment(
            database,
            "alice",
            "test",
            "",
            chunks(b"abc"),
            expected_size=3,
            check_grant=change,
        )
    assert recover_attachment_uploads(database, stale_seconds=0) == 0
    assert (
        next((database.root / "attachments").rglob("file_*")).read_bytes()
        == b"xyz"
    )
    with database.connect() as db:
        assert not db.execute("SELECT * FROM attachments").fetchall()
        assert db.execute("SELECT * FROM attachment_uploads").fetchone()
