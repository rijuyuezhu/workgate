"""Control-owned attachment journal reconciliation without new authority."""

import asyncio
import os
import sqlite3
from pathlib import Path
from threading import Event

import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.http import hosted_chat
from workgate.control.http.app import build_http_app
from workgate.hosted_chat import attachment_recovery
from workgate.hosted_chat.attachment_recovery import recover_private_attachments
from workgate.hosted_chat.attachment_uploads import publish_attachment
from workgate.hosted_chat.database import SQLiteDatabase


@pytest.mark.asyncio
async def test_lifecycle_recovers_existing_journals(tmp_path):
    runtime = runtime_for(tmp_path)
    database = SQLiteDatabase(runtime.config.state_dir)

    async def chunks():
        yield b"original"

    file = await publish_attachment(
        database,
        "owner",
        "a.txt",
        "text/plain",
        chunks(),
        expected_size=8,
        check_grant=lambda: None,
    )
    ident = file["file_id"]
    with database.connect() as db:
        original = Path(
            db.execute(
                "SELECT path FROM attachments WHERE id=?", (ident,)
            ).fetchone()["path"]
        )
        quarantine = original.with_name(".delete-recovery")
        os.link(original, quarantine)
        original.unlink()
        db.execute(
            "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
            (ident, "owner", str(original), str(quarantine), "prepared"),
        )
        db.execute(
            "INSERT INTO attachment_uploads(file_id,owner,path,created_at) VALUES(?,?,?,0)",
            ("stale", "owner", str(original.with_name("stale"))),
        )
    build_http_app(runtime=runtime)
    async with runtime.lifespan():
        async with asyncio.timeout(5):
            while quarantine.exists():
                await asyncio.sleep(0.01)
        assert original.read_bytes() == b"original"
        with database.connect() as db:
            assert not db.execute("SELECT 1 FROM attachment_uploads").fetchall()
    with database.connect() as db:
        assert not db.execute("SELECT 1 FROM attachment_deletions").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,ui_enabled", [(True, True), (False, True), (True, False)]
)
async def test_recovery_does_not_initialize_unused_database(
    tmp_path, enabled, ui_enabled, monkeypatch
):
    runtime = runtime_for(tmp_path, enabled=enabled, ui_enabled=ui_enabled)
    calls = []
    monkeypatch.setattr(
        hosted_chat, "recover_private_attachments", calls.append
    )
    build_http_app(runtime=runtime)
    async with runtime.lifespan():
        await asyncio.sleep(0.05)
        assert not (runtime.config.state_dir / "hosted-chat").exists()
        assert not calls


def test_independent_ledgers_and_empty_pass(tmp_path, monkeypatch, caplog):
    database = SQLiteDatabase(tmp_path)
    calls = []

    def fail(db):
        calls.append("upload")
        raise sqlite3.DatabaseError("private path")

    def delete(self):
        calls.append("delete")
        return {}

    monkeypatch.setattr(attachment_recovery, "recover_attachment_uploads", fail)
    monkeypatch.setattr(
        attachment_recovery.AttachmentDeletionStore, "recover", delete
    )
    recover_private_attachments(database)
    assert not calls
    with database.connect() as db:
        db.execute(
            "INSERT INTO attachment_uploads(file_id,owner,path) VALUES(?,?,?)",
            ("a", "owner", "a"),
        )
        db.execute(
            "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,?)",
            ("b", "owner", "b", ".delete-b", "prepared"),
        )
    recover_private_attachments(database)
    assert calls == ["upload", "delete"]
    assert "private path" not in caplog.text


@pytest.mark.asyncio
async def test_shutdown_drains_recovery(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path)
    SQLiteDatabase(runtime.config.state_dir)
    entered, release, finished = Event(), Event(), Event()
    calls = []

    def recover(database):
        calls.append(database)
        entered.set()
        assert release.wait(5)
        finished.set()

    monkeypatch.setattr(hosted_chat, "recover_private_attachments", recover)
    build_http_app(runtime=runtime)
    await runtime.start()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        runtime.generation_supervisor.wake_cleanup()
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0.05)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, timeout=5)
        assert finished.is_set()
        assert len(calls) == 1
    finally:
        release.set()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_generation_wakes_do_not_rescan_journals(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path)
    SQLiteDatabase(runtime.config.state_dir)
    sweeps = asyncio.Queue()
    recovered = Event()
    calls = []

    async def sweep(*_args):
        sweeps.put_nowait(None)

    def recover(database):
        calls.append(database)
        recovered.set()

    monkeypatch.setattr(hosted_chat, "sweep_native_cancellations", sweep)
    monkeypatch.setattr(hosted_chat, "recover_private_attachments", recover)
    build_http_app(runtime=runtime)
    async with runtime.lifespan():
        await asyncio.wait_for(sweeps.get(), timeout=5)
        assert await asyncio.to_thread(recovered.wait, 5)
        runtime.generation_supervisor.wake_cleanup()
        await asyncio.wait_for(sweeps.get(), timeout=5)
        await asyncio.sleep(0.05)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_disabled_host_does_not_recover_existing_database(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path, enabled=False)
    SQLiteDatabase(runtime.config.state_dir)
    calls = []
    monkeypatch.setattr(
        hosted_chat, "recover_private_attachments", calls.append
    )
    build_http_app(runtime=runtime)
    async with runtime.lifespan():
        await asyncio.sleep(0.05)
        assert not calls
