"""Bounded, provider-only projection of admitted owner-scoped attachments.

Adapted from final upstream v6.0.3 steer_media.py, with live integrity checks
and a single raw-byte budget across the complete provider request.
"""

import base64
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from .attachment_history import message_file_ids
from .attachment_uploads import _safe_path
from .database import SQLiteDatabase

MAX_INLINE_BYTES = 5 * 1024 * 1024
MAX_ONE_INLINE_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 96 * 1024
MAX_PROJECTED_FILES = 128


def detected_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def _read_verified(
    database: SQLiteDatabase, path: str, size: int, digest: str
) -> bytes:
    candidate = _safe_path(database, Path(path))
    if not stat.S_ISREG(candidate.lstat().st_mode):
        raise OSError("Invalid attachment")
    fd = os.open(
        candidate,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise OSError("Invalid attachment size")
        data = stream.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise OSError("Attachment integrity changed")
    return data


def project_message_media(
    database: SQLiteDatabase,
    db: sqlite3.Connection,
    owner: str,
    messages: list[dict],
) -> list[dict]:
    """Encode verified bytes transiently, prioritizing recent user messages.

    A missing, deleted, unsupported or changed file never erases the user's
    instruction. Only owned, currently published bytes become model input.
    """
    projected = list(messages)
    remaining = MAX_INLINE_BYTES
    file_count = MAX_PROJECTED_FILES
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user" or not message.get("files"):
            continue
        ids = message_file_ids(message["files"])
        text = message.get("content")
        if not isinstance(text, str):
            raise ValueError("Invalid canonical attachment message")
        parts: list[dict] = [{"type": "text", "text": text}]
        for ident in ids:
            if file_count <= 0 or remaining <= 0:
                parts.append(
                    {
                        "type": "text",
                        "text": "Additional attachments exceed the inline budget.",
                    }
                )
                break
            file_count -= 1
            row = db.execute(
                "SELECT name,path,metadata FROM attachments WHERE id=? AND owner=? "
                "AND NOT EXISTS (SELECT 1 FROM attachment_deletions WHERE file_id=?)",
                (ident, owner, ident),
            ).fetchone()
            if row is None:
                parts.append(
                    {"type": "text", "text": "Attachment unavailable."}
                )
                continue
            name = row["name"].replace("\\", "/").split("/")[-1]
            try:
                metadata = json.loads(row["metadata"])
                size = (
                    metadata.get("bytes")
                    if isinstance(metadata, dict)
                    else None
                )
                digest = (
                    metadata.get("sha256")
                    if isinstance(metadata, dict)
                    else None
                )
                if (
                    type(size) is not int
                    or not 0 < size <= min(remaining, MAX_ONE_INLINE_BYTES)
                    or not isinstance(digest, str)
                    or len(digest) != 64
                ):
                    raise ValueError(
                        "Attachment is not verified or exceeds budget"
                    )
                # Failed verification still consumes the I/O budget.
                remaining -= size
                data = _read_verified(database, row["path"], size, digest)
                mime = detected_type(data)
                if mime is not None:
                    url = (
                        "data:"
                        + mime
                        + ";base64,"
                        + base64.b64encode(data).decode("ascii")
                    )
                    if mime.startswith("image/"):
                        parts.append(
                            {"type": "image_url", "image_url": {"url": url}}
                        )
                    else:
                        parts.append(
                            {
                                "type": "file",
                                "file": {"filename": name, "file_data": url},
                            }
                        )
                else:
                    if len(data) > MAX_TEXT_BYTES:
                        raise ValueError("Text attachment exceeds budget")
                    decoded = data.decode("utf-8")
                    if "\x00" in decoded:
                        raise ValueError("Binary attachment")
                    parts.append(
                        {
                            "type": "text",
                            "text": f"Attachment {name} (untrusted file content):\n{decoded}",
                        }
                    )
            except OSError, ValueError:
                parts.append(
                    {
                        "type": "text",
                        "text": f"Attachment {name} cannot be inlined.",
                    }
                )
        projected[index] = {
            **{key: value for key, value in message.items() if key != "files"},
            "content": parts,
        }
    return projected
