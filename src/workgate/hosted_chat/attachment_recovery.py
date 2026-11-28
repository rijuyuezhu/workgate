"""Reconcile private attachment journals without issuing execution grants."""

import logging
import sqlite3

from .attachment_deletion import AttachmentDeletionStore
from .attachment_uploads import recover_attachment_uploads
from .database import SQLiteDatabase

log = logging.getLogger(__name__)


def recover_private_attachments(database: SQLiteDatabase) -> None:
    """Retry each ledger independently, leaving uncertain bytes untouched."""
    for table, recover in (
        ("attachment_uploads", lambda: recover_attachment_uploads(database)),
        ("attachment_deletions", AttachmentDeletionStore(database).recover),
    ):
        try:
            with database.connect() as db:
                pending = db.execute(
                    f"SELECT 1 FROM {table} LIMIT 1"
                ).fetchone()
            if pending is not None:
                recover()
        except (OSError, sqlite3.DatabaseError) as error:
            log.warning(
                "Attachment journal recovery deferred (%s)",
                type(error).__name__,
            )
