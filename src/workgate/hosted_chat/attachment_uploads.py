"""Private attachment publication and recovery, adapted from pinned v6.0.3."""

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import AsyncIterable, Callable
from datetime import UTC, datetime
from pathlib import Path

from workgate.app_paths import ensure_private_directory
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.utils.private_files import private_file_lock

MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
log = logging.getLogger(__name__)


def _identity(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}"


def _safe_path(database: SQLiteDatabase, path: Path) -> Path:
    root = database.root.resolve() / "attachments"
    if root not in path.parents or path.resolve() != path:
        raise OSError("Unsafe attachment path")
    return path


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _target(database: SQLiteDatabase, file_id: str) -> Path:
    now = datetime.now(UTC)
    parent = (
        database.root.resolve()
        / "attachments"
        / f"{now.year:04d}"
        / f"{now.month:02d}"
    )
    path = _safe_path(database, parent / file_id)
    for directory in (parent.parent.parent, parent.parent, parent):
        ensure_private_directory(directory)
    return _safe_path(database, path)


def _matches(path: Path, identity: str, size: int, digest: str) -> bool:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or _identity(info) != identity
        or info.st_size != size
    ):
        return False
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if _identity(before) != identity or before.st_size != size:
            return False
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    return actual == digest and _identity(path.lstat()) == identity


def _reclaim(
    database: SQLiteDatabase, db: sqlite3.Connection, intent: sqlite3.Row
) -> bool:
    """Remove only an absent or positively identified uncommitted file."""
    ident, owner = intent["file_id"], intent["owner"]
    path = _safe_path(database, Path(intent["path"]))
    if db.execute(
        "SELECT 1 FROM attachments WHERE id=? OR path=?", (ident, str(path))
    ).fetchone():
        db.execute(
            "DELETE FROM attachment_uploads WHERE file_id=? AND owner=?",
            (ident, owner),
        )
        return True
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        identity, size, digest = (
            intent["file_identity"],
            intent["content_size"],
            intent["content_sha256"],
        )
        if (
            identity is None
            or size is None
            or digest is None
            or not _matches(path, identity, size, digest)
        ):
            return False
        path.unlink()
        _sync_directory(path.parent)
    db.execute(
        "DELETE FROM attachment_uploads WHERE file_id=? AND owner=?",
        (ident, owner),
    )
    return True


def recover_attachment_uploads(
    database: SQLiteDatabase, *, stale_seconds: int = 3600
) -> int:
    """Conservatively recover expired intents; uncertain bytes remain untouched."""
    if stale_seconds < 0:
        raise ValueError("Invalid recovery age")
    recovered = 0
    with (
        private_file_lock(
            database.root / "attachment-upload.lock", timeout_s=30
        ),
        database.connect() as db,
    ):
        db.execute("BEGIN IMMEDIATE")
        intents = db.execute(
            "SELECT * FROM attachment_uploads WHERE created_at <= ?",
            (int(time.time()) - stale_seconds,),
        ).fetchall()
        for intent in intents:
            try:
                recovered += int(_reclaim(database, db, intent))
            except OSError:
                log.warning("Attachment recovery retained an uncertain file")
    return recovered


async def publish_attachment(
    database: SQLiteDatabase,
    owner: str,
    name: str,
    content_type: str,
    chunks: AsyncIterable[bytes],
    *,
    expected_size: int,
    check_grant: Callable[[], None],
    process: Callable[[Path, str, str], dict] | None = None,
    bind: Callable[[sqlite3.Connection, str], object] | None = None,
) -> dict:
    """Commit owner metadata only after durable, bounded, verified file bytes."""
    if not owner or not isinstance(name, str) or not 1 <= len(name) <= 200:
        raise ValueError("Invalid attachment owner or name")
    name = name.replace("\\", "/").split("/")[-1]
    if name in {"", ".", ".."} or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in name
    ):
        raise ValueError("Invalid attachment name")
    if (
        type(expected_size) is not int
        or not 0 < expected_size <= MAX_ATTACHMENT_BYTES
    ):
        raise ValueError("Invalid attachment size")
    if (
        not content_type
        or len(content_type) > 256
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in content_type)
    ):
        content_type = "application/octet-stream"
    check_grant()
    ident = "file_" + uuid.uuid4().hex
    path = _target(database, ident)
    lock = database.root / "attachment-upload.lock"
    with database.connect() as db:
        db.execute(
            "INSERT INTO attachment_uploads(file_id,owner,path) VALUES(?,?,?)",
            (ident, owner, str(path)),
        )
    identity = None
    size = 0
    digest = hashlib.sha256()
    try:
        with private_file_lock(lock, timeout_s=30):
            with database.connect() as db:
                if not db.execute(
                    "SELECT 1 FROM attachment_uploads WHERE file_id=? AND owner=?",
                    (ident, owner),
                ).fetchone():
                    raise OSError("Upload intent was reclaimed")
            descriptor = os.open(
                _safe_path(database, path),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            handle = os.fdopen(descriptor, "wb")
        with handle:
            identity = _identity(os.fstat(handle.fileno()))
            _sync_directory(path.parent)
            async for chunk in chunks:
                check_grant()
                if size + len(chunk) > expected_size:
                    raise ValueError("Attachment exceeds declared size")
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if size != expected_size:
            raise ValueError("Incomplete attachment upload")
        with database.connect() as db:
            if not db.execute(
                "UPDATE attachment_uploads SET file_identity=?,content_size=?,content_sha256=? WHERE file_id=? AND owner=? AND path=?",
                (identity, size, digest.hexdigest(), ident, owner, str(path)),
            ).rowcount:
                raise OSError("Upload intent was reclaimed")
        processed = {}
        if process is not None:
            check_grant()
            if not _matches(
                _safe_path(database, path), identity, size, digest.hexdigest()
            ):
                raise OSError("Upload bytes changed before processing")
            processed = await asyncio.to_thread(
                process, path, name, content_type
            )
        with private_file_lock(lock, timeout_s=30), database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_grant()
            if not _matches(
                _safe_path(database, path), identity, size, digest.hexdigest()
            ):
                raise OSError("Upload bytes changed before publication")
            if not db.execute(
                "DELETE FROM attachment_uploads WHERE file_id=? AND owner=? AND path=? AND file_identity=? AND content_size=? AND content_sha256=?",
                (ident, owner, str(path), identity, size, digest.hexdigest()),
            ).rowcount:
                raise OSError("Upload intent was reclaimed")
            db.execute(
                "INSERT INTO attachments(id,owner,name,path,content_type,metadata) VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    owner,
                    name,
                    str(path),
                    content_type,
                    json.dumps(
                        {
                            **processed,
                            "bytes": size,
                            "machine": "local",
                            "sha256": digest.hexdigest(),
                        }
                    ),
                ),
            )
            if bind is not None:
                bind(db, ident)
                check_grant()
        return {
            "file_id": ident,
            "filename": name,
            "bytes": size,
            "type": content_type,
        }
    except BaseException:
        # Cancellation and commit uncertainty use the same conservative cleanup.
        try:
            with (
                private_file_lock(lock, timeout_s=30),
                database.connect() as db,
            ):
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE attachment_uploads SET file_identity=?,content_size=?,content_sha256=? WHERE file_id=? AND owner=?",
                    (identity, size, digest.hexdigest(), ident, owner),
                )
                intent = db.execute(
                    "SELECT * FROM attachment_uploads WHERE file_id=? AND owner=?",
                    (ident, owner),
                ).fetchone()
                if intent is not None:
                    _reclaim(database, db, intent)
        except OSError, sqlite3.DatabaseError:
            log.warning(
                "Attachment upload outcome uncertain; retained for recovery"
            )
        raise
