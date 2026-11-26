"""Hosted Chat database isolation, snapshot and final-schema contracts."""

import concurrent.futures
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from workgate.hosted_chat import database
from workgate.hosted_chat.database import DB_NAME, SQLiteDatabase


def test_parallel_cold_sqlite_startup_serializes_wal_schema(
    tmp_path: Path,
) -> None:
    """Separate threads must not race WAL/schema initialization."""
    import threading

    ready = threading.Barrier(8)

    def initialize(_: int) -> Path:
        ready.wait(timeout=10)
        return SQLiteDatabase(tmp_path).path

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(initialize, range(8)))
    assert paths == [tmp_path / "hosted-chat" / DB_NAME] * 8


def test_chat_schema_is_private_and_separate_from_native_state(
    tmp_path: Path,
) -> None:
    native = tmp_path / "state.json"
    native.write_bytes(b"native bytes")
    unrelated = tmp_path / "lsm.sqlite3"
    unrelated.write_bytes(b"upstream state must never be opened")
    db = SQLiteDatabase(tmp_path)
    assert db.path == tmp_path / "hosted-chat" / DB_NAME
    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "conversations",
            "messages",
            "projects",
            "agents",
            "settings",
            "runs",
            "generation_events",
            "attachments",
            "agent_durable_tasks",
            "subagent_tasks",
            "action_records",
            "agent_event_deliveries",
        } <= tables
        assert (
            not {
                "state",
                "state_append_chunks",
                "state_append_lengths",
                "legacy_imports",
            }
            & tables
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0x57474348
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        if os.name != "nt":
            assert stat.S_IMODE(db.root.stat().st_mode) == 0o700
            assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
            for suffix in ("-wal", "-shm"):
                path = db.path.with_name(DB_NAME + suffix)
                if path.exists():
                    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert native.read_bytes() == b"native bytes"
    assert unrelated.read_bytes() == b"upstream state must never be opened"


def test_transactions_commit_and_rollback(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO settings(owner,key,value) VALUES('owner','test','1')"
        )
    with pytest.raises(RuntimeError, match="rollback"), db.connect() as conn:
        conn.execute("UPDATE settings SET value='2' WHERE owner='owner'")
        raise RuntimeError("rollback")
    with SQLiteDatabase(tmp_path).connect() as conn:
        assert conn.execute("SELECT value FROM settings").fetchone()[0] == "1"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages(id,conversation_id,role) VALUES('message','missing','user')"
            )


def test_read_snapshot_does_not_mix_concurrent_updates(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO settings(owner,key,value) VALUES('owner','test','1')"
        )
    with db.read_snapshot() as reader:
        assert reader.execute("SELECT value FROM settings").fetchone()[0] == "1"
        with db.connect() as writer:
            writer.execute("UPDATE settings SET value='2' WHERE owner='owner'")
        assert reader.execute("SELECT value FROM settings").fetchone()[0] == "1"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM settings")
    with db.read_snapshot() as reader:
        assert reader.execute("SELECT value FROM settings").fetchone()[0] == "2"


def test_missing_fts_is_atomic_and_falls_back(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        database,
        "_FTS_SCHEMA",
        "CREATE VIRTUAL TABLE message_search USING unavailable_test_module(x);",
    )

    # Model the SQLite distribution's missing FTS5 module specifically.
    # Use the same recognized absence error as a build without FTS5.
    class MissingFTS(sqlite3.Connection):
        def executescript(self, sql):
            if "unavailable_test_module" in sql:
                super().executescript(sql.split("CREATE VIRTUAL TABLE", 1)[0])
                raise sqlite3.OperationalError("no such module: fts5")
            return super().executescript(sql)

    connect = sqlite3.connect
    monkeypatch.setattr(
        database.sqlite3,
        "connect",
        lambda *args, **kwargs: connect(*args, factory=MissingFTS, **kwargs),
    )
    db = SQLiteDatabase(tmp_path)
    assert not db.fts_enabled
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='message_search'"
            ).fetchone()
            is None
        )


def test_fts_message_projection_if_supported(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path)
    if not db.fts_enabled:
        pytest.skip("SQLite build lacks trigram FTS5")
    with db.connect() as conn:
        conn.execute("INSERT INTO conversations(id,owner) VALUES('c','owner')")
        conn.execute(
            "INSERT INTO messages(id,conversation_id,role,text) VALUES('m','c','user','SQLite 中文全文检索')"
        )
        assert (
            conn.execute(
                "SELECT message_id FROM message_search WHERE text MATCH ?",
                ('"中文全"',),
            ).fetchone()[0]
            == "m"
        )
        conn.execute("UPDATE messages SET text='changed' WHERE id='m'")
        assert (
            conn.execute(
                "SELECT message_id FROM message_search WHERE text MATCH ?",
                ('"中文全"',),
            ).fetchone()
            is None
        )
        conn.execute("DELETE FROM conversations WHERE id='c'")
        assert (
            conn.execute("SELECT COUNT(*) FROM message_search").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("version", [0, 1, 2])
def test_unrecognized_database_is_not_adopted(
    tmp_path: Path, version: int
) -> None:
    root = tmp_path / "hosted-chat"
    root.mkdir(mode=0o700)
    path = root / DB_NAME
    path.touch(mode=0o600)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE other(value TEXT)")
        conn.execute(f"PRAGMA user_version={version}")
    with pytest.raises(RuntimeError, match="[Uu]nrecognized|[Uu]nsupported"):
        SQLiteDatabase(tmp_path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='conversations'"
            ).fetchone()
            is None
        )


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission and symlink semantics"
)
def test_database_rejects_nonprivate_or_linked_file(tmp_path: Path) -> None:
    root = tmp_path / "hosted-chat"
    root.mkdir(mode=0o700)
    target = tmp_path / "native.json"
    target.write_bytes(b"untouched")
    path = root / DB_NAME
    path.symlink_to(target)
    with pytest.raises(OSError, match="regular private"):
        SQLiteDatabase(tmp_path)
    assert target.read_bytes() == b"untouched"
    path.unlink()
    path.touch(mode=0o600)
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="owner-only"):
        SQLiteDatabase(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX link semantics")
def test_linked_directory_and_schema_lock_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    root = tmp_path / "hosted-chat"
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="directory must not be a link"):
        SQLiteDatabase(tmp_path)
    assert not list(target.iterdir())
    root.unlink()
    root.mkdir(mode=0o700)
    marker = target / "marker"
    marker.write_bytes(b"untouched")
    os.link(marker, root / "schema.lock")
    with pytest.raises(OSError, match="regular private"):
        SQLiteDatabase(tmp_path)
    assert marker.read_bytes() == b"untouched"


def test_unexpected_schema_failure_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(database, "_FTS_SCHEMA", "CREATE TABLE broken(;")
    with pytest.raises(sqlite3.OperationalError):
        SQLiteDatabase(tmp_path)
    path = tmp_path / "hosted-chat" / DB_NAME
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
