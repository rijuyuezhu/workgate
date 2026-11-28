"""Owner-scoped journaled deletion adapted from pinned v6.0.3."""

import json
import logging
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from workgate.hosted_chat.attachment_uploads import _safe_path, _sync_directory
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.utils.private_files import private_file_lock

log = logging.getLogger(__name__)


class AttachmentDeletionStore:
    """Keep durable deletion intent until metadata and bytes agree."""

    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def _lock(self):
        return private_file_lock(
            self.database.root / "attachment-deletion.lock", timeout_s=30
        )

    def _paths(self, intent: sqlite3.Row) -> tuple[Path, Path]:
        original = _safe_path(self.database, Path(intent["original_path"]))
        quarantine = _safe_path(self.database, Path(intent["quarantine_path"]))
        if (
            quarantine.parent != original.parent
            or not quarantine.name.startswith(".delete-")
        ):
            raise OSError("Unsafe deletion quarantine")
        return original, quarantine

    @staticmethod
    def _present(path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Attachment is not a regular file")
        return True

    def _recover_one(self, file_id: str) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            intent = db.execute(
                "SELECT * FROM attachment_deletions WHERE file_id=?", (file_id,)
            ).fetchone()
            if intent is None:
                return
            original, quarantine = self._paths(intent)
            row = db.execute(
                "SELECT owner,path FROM attachments WHERE id=?", (file_id,)
            ).fetchone()
            if row is not None:
                if (
                    row["owner"] != intent["owner"]
                    or row["path"] != str(original)
                    or intent["phase"] != "prepared"
                ):
                    raise OSError("Attachment metadata changed during deletion")
                if self._present(quarantine):
                    if self._present(original):
                        if not original.samefile(quarantine):
                            raise OSError("Deletion recovery collision")
                    else:
                        os.link(quarantine, original, follow_symlinks=False)
                        _sync_directory(original.parent)
                    quarantine.unlink()
                    _sync_directory(quarantine.parent)
                elif not self._present(original):
                    raise OSError("Missing both attachment and quarantine")
            elif intent["phase"] != "committed":
                raise OSError(
                    "Uncertain deletion metadata; quarantine retained"
                )
            elif self._present(quarantine):
                quarantine.unlink()
                _sync_directory(quarantine.parent)
            db.execute(
                "DELETE FROM attachment_deletions WHERE file_id=?", (file_id,)
            )

    def recover(self) -> dict:
        recovered, failed = [], []
        with self._lock():
            with self.database.connect() as db:
                ids = [
                    row["file_id"]
                    for row in db.execute(
                        "SELECT file_id FROM attachment_deletions ORDER BY created_at,file_id"
                    )
                ]
            for file_id in ids:
                try:
                    self._recover_one(file_id)
                    recovered.append(file_id)
                except OSError, sqlite3.DatabaseError:
                    log.warning("Attachment deletion recovery deferred")
                    failed.append(file_id)
        return {"recoveredFileIds": recovered, "failedFileIds": failed}

    def _prepare(
        self, owner: str, file_id: str, check_grant: Callable[[], None]
    ) -> bool:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_grant()
            row = db.execute(
                "SELECT owner,path FROM attachments WHERE id=?", (file_id,)
            ).fetchone()
            if row is None:
                return False
            if row["owner"] != owner:
                raise PermissionError("Attachment owner mismatch")
            original = _safe_path(self.database, Path(row["path"]))
            quarantine = original.with_name(".delete-" + uuid.uuid4().hex)
            if db.execute(
                "SELECT 1 FROM attachment_deletions WHERE file_id=?", (file_id,)
            ).fetchone():
                raise OSError("Deletion requires recovery")
            db.execute(
                "INSERT INTO attachment_deletions(file_id,owner,original_path,quarantine_path,phase) VALUES(?,?,?,?,'prepared')",
                (file_id, owner, str(original), str(quarantine)),
            )
            return True

    def _retire_references(
        self, db: sqlite3.Connection, owner: str, file_id: str
    ) -> None:
        # Tool attachments are retired by the normalized FK cascade. Message
        # history resolves file IDs against the current owned attachment table.
        # Agent resource authoring arrives in L2; never silently orphan a
        # resource on an Agent imported from an older database.
        for row in db.execute(
            "SELECT config FROM agents WHERE owner=?", (owner,)
        ):
            resources = json.loads(row["config"]).get("tool_resources", {})
            if isinstance(resources, dict) and any(
                isinstance(value, dict) and file_id in value.get("file_ids", [])
                for value in resources.values()
                if isinstance(value, dict)
                and isinstance(value.get("file_ids"), list)
            ):
                raise OSError("Attachment is referenced by an Agent")

    def _finish(
        self, owner: str, file_id: str, check_grant: Callable[[], None]
    ) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_grant()
            intent = db.execute(
                "SELECT * FROM attachment_deletions WHERE file_id=? AND owner=? AND phase='prepared'",
                (file_id, owner),
            ).fetchone()
            row = db.execute(
                "SELECT path FROM attachments WHERE id=? AND owner=?",
                (file_id, owner),
            ).fetchone()
            if (
                intent is None
                or row is None
                or row["path"] != intent["original_path"]
            ):
                raise OSError("Attachment metadata changed before deletion")
            original, quarantine = self._paths(intent)
            self._retire_references(db, owner, file_id)
            if self._present(quarantine):
                raise OSError("Deletion quarantine collision")
            if self._present(original):
                # Hardlink/unlink never overwrites an independently created
                # quarantine. Recovery handles the two-link crash window.
                os.link(original, quarantine, follow_symlinks=False)
                _sync_directory(original.parent)
                original.unlink()
                _sync_directory(original.parent)
            db.execute(
                "DELETE FROM attachments WHERE id=? AND owner=?",
                (file_id, owner),
            )
            db.execute(
                "UPDATE attachment_deletions SET phase='committed' WHERE file_id=?",
                (file_id,),
            )

    def delete(
        self,
        owner: str,
        identities: Sequence[str],
        *,
        check_grant: Callable[[], None],
    ) -> dict:
        """Reject a foreign owner anywhere in the batch before moving any bytes."""
        if (
            not owner
            or len(identities) > 500
            or any(
                not isinstance(ident, str) or not ident for ident in identities
            )
        ):
            raise ValueError("Invalid attachment deletion request")
        deleted, failed = [], []
        with self._lock():
            with self.database.connect() as db:
                check_grant()
                for ident in dict.fromkeys(identities):
                    row = db.execute(
                        "SELECT owner FROM attachments WHERE id=?", (ident,)
                    ).fetchone()
                    if row is not None and row["owner"] != owner:
                        raise PermissionError("Attachment owner mismatch")
            for ident in dict.fromkeys(identities):
                try:
                    if not self._prepare(owner, ident, check_grant):
                        continue
                    self._finish(owner, ident, check_grant)
                except BaseException as exc:
                    # A commit acknowledgement can fail after SQLite commits.
                    # Reconcile durable state rather than assume rollback.
                    try:
                        with self.database.connect() as db:
                            row = db.execute(
                                "SELECT phase FROM attachment_deletions WHERE file_id=? AND owner=?",
                                (ident, owner),
                            ).fetchone()
                        if row is not None:
                            self._recover_one(ident)
                            if row["phase"] == "committed":
                                deleted.append(ident)
                                continue
                    except OSError, sqlite3.DatabaseError:
                        log.warning("Attachment deletion recovery deferred")
                    if isinstance(exc, PermissionError) or not isinstance(
                        exc, (OSError, sqlite3.DatabaseError)
                    ):
                        raise
                    failed.append(ident)
                    continue
                deleted.append(ident)
                try:
                    self._recover_one(ident)
                except OSError, sqlite3.DatabaseError:
                    log.warning(
                        "Committed attachment quarantine cleanup deferred"
                    )
        return {"deletedFileIds": deleted, "failedFileIds": failed}
