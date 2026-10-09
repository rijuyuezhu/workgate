"""Bounded, self-indexing compressed history for evicted canonical Audit records."""

import contextlib
import gzip
import io
import json
import re
import stat
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

from ..app_paths import ensure_private_directory
from ..config.role_config import SharedRoleConfig
from ..persistence import StateLayout
from ..utils.private_files import atomic_write_private_bytes
from .payloads import _read_regular_file_nofollow

_ARCHIVE_NAME = re.compile(r"^\d{20}-[0-9a-f]{12}\.jsonl\.gz$")
_MAX_ARCHIVE_SEGMENTS = 512


def _root(settings: SharedRoleConfig) -> Path:
    return StateLayout(settings.state_dir).audit_dir / "archives"


def archive_paths(settings: SharedRoleConfig) -> list[Path]:
    """Discover committed archives; the directory is the index after restart."""
    root = _root(settings)
    if not root.is_dir() or root.is_symlink():
        return []
    paths: list[Path] = []
    for path in root.iterdir():
        if not _ARCHIVE_NAME.fullmatch(path.name):
            continue
        with contextlib.suppress(OSError):
            if stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink():
                paths.append(path)
    return sorted(paths)


def archive_evicted(lines: list[bytes], settings: SharedRoleConfig) -> None:
    """Commit a private segment before any destructive hot-log replacement."""
    if not lines or settings.max_audit_archive_bytes == 0:
        return
    encoded = gzip.compress(b"".join(lines), compresslevel=6, mtime=0)
    if (
        len(encoded) > settings.max_audit_archive_bytes
        or sum(map(len, lines)) > settings.max_audit_archive_bytes
    ):
        raise OSError(
            "evicted audit unit exceeds the configured cold archive budget"
        )
    root = _root(settings)
    ensure_private_directory(root)
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex[:12]}.jsonl.gz"
    atomic_write_private_bytes(root / name, encoded)


def read_archive(
    settings: SharedRoleConfig, path: Path
) -> list[dict[str, Any]]:
    """Bound decompression and tolerate incomplete or corrupt archive files."""
    limit = max(1, settings.max_audit_archive_bytes)
    try:
        compressed = _read_regular_file_nofollow(
            path, max(1, settings.max_audit_archive_bytes)
        )
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            raw = stream.read(limit + 1)
            if len(raw) > limit or stream.read(1):
                return []
        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            with contextlib.suppress(
                ValueError, UnicodeDecodeError, RecursionError
            ):
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
        return records
    except OSError, EOFError, ValueError, zlib.error, RecursionError:
        return []


def archived_records(settings: SharedRoleConfig) -> list[dict[str, Any]]:
    """Recover all valid retained cold records without resolving payload objects."""
    if settings.max_audit_archive_bytes == 0:
        return []
    return [
        row
        for path in archive_paths(settings)
        for row in read_archive(settings, path)
    ]


def prune_archives(settings: SharedRoleConfig) -> list[dict[str, Any]]:
    """Reconcile files and prune oldest segments after committing the hot log.

    The same budget caps compressed disk usage and uncompressed query material.
    An absent index cannot hide valid segments; filenames are the authoritative list.
    """
    paths = archive_paths(settings)
    archives = [(path, read_archive(settings, path)) for path in paths]
    compressed_bytes = sum(path.stat().st_size for path in paths)
    for path, records in archives:
        if not records:
            with contextlib.suppress(OSError):
                compressed_bytes -= path.stat().st_size
                path.unlink()
    archives = [(path, rows) for path, rows in archives if rows]
    raw_bytes = sum(
        len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
        for _, rows in archives
        for row in rows
    )
    while archives and (
        compressed_bytes > settings.max_audit_archive_bytes
        or raw_bytes > settings.max_audit_archive_bytes
        or len(archives) > _MAX_ARCHIVE_SEGMENTS
    ):
        oldest, records = archives.pop(0)
        raw_bytes -= sum(
            len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
            for row in records
        )
        with contextlib.suppress(OSError):
            compressed_bytes -= oldest.stat().st_size
            oldest.unlink()
    return [record for _, records in archives for record in records]
