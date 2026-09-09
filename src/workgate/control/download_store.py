"""Durable private state for tokenized file-link snapshots."""

import contextlib
import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from ..audit import audit
from ..persistence import get_state_store
from ..utils.private_files import (
    atomic_write_private_text,
    private_file_lock,
)
from .download_snapshot import (
    SNAPSHOT_SUFFIX,
    STAGING_SUFFIX,
    snapshot_directory,
)

STORE_VERSION = 2
LEGACY_STORE_VERSIONS = {1}
STORE_FILE_NAME = "downloads.json"
BACKUP_FILE_NAME = "downloads.json.bak"
LOCK_FILE_NAME = "downloads.lock"
STORE_LOCK = threading.RLock()
STAGING_PRUNE_GRACE_S = 3600


@dataclass(frozen=True)
class ClaimedDownload:
    """One validated open snapshot claimed for an HTTP response."""

    handle: BinaryIO
    """Already-open private snapshot handle used for streaming."""

    path: Path
    """Opaque private snapshot path used for final cleanup."""

    link: dict[str, Any]
    """Validated metadata copied from the durable link store."""

    remove_snapshot_after: bool
    """Whether response completion should delete the snapshot bytes."""


def store_path() -> Path:
    """Return the primary download-link metadata path."""
    return get_state_store().layout.downloads_store_path


def backup_path() -> Path:
    """Return the fallback download-link metadata path."""
    return get_state_store().layout.downloads_store_backup_path


def lock_path() -> Path:
    """Return the cross-process download-link lock path."""
    return get_state_store().layout.downloads_lock_path


def empty_store() -> dict[str, Any]:
    """Return one empty current-version store."""
    return {"version": STORE_VERSION, "links": {}}


def _load_file(path: Path) -> tuple[dict[str, Any], bool]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unsupported or invalid download store: {path}")
    version = data.get("version")
    if version not in {STORE_VERSION, *LEGACY_STORE_VERSIONS}:
        raise ValueError(f"unsupported or invalid download store: {path}")
    links = data.get("links")
    if not isinstance(links, dict):
        raise ValueError(f"download store links field is invalid: {path}")
    normalized = {
        str(token): link
        for token, link in links.items()
        if isinstance(token, str) and isinstance(link, dict)
    }
    if version in LEGACY_STORE_VERSIONS:
        audit(
            "download_store_migrated",
            path=str(path),
            from_version=version,
            to_version=STORE_VERSION,
            dropped_links=len(normalized),
        )
        return empty_store(), True
    return {"version": STORE_VERSION, "links": normalized}, False


def save_locked(store: dict[str, Any]) -> None:
    """Atomically persist the primary store and a recovery copy."""
    store["version"] = STORE_VERSION
    payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_private_text(store_path(), payload)
    try:
        atomic_write_private_text(backup_path(), payload)
    except OSError as exc:
        audit(
            "download_store_backup_write_failed",
            path=str(backup_path()),
            error=repr(exc),
        )


def load_locked() -> dict[str, Any]:
    """Load primary state or recover from backup without silent reset."""
    primary = store_path()
    backup = backup_path()
    if not primary.exists() and not backup.exists():
        return empty_store()

    primary_error: Exception | None = None
    if primary.exists():
        try:
            store, migrated = _load_file(primary)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            primary_error = exc
            audit(
                "download_store_unreadable", path=str(primary), error=repr(exc)
            )
        else:
            if migrated:
                save_locked(store)
            return store

    if backup.exists():
        try:
            store, _migrated = _load_file(backup)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            audit(
                "download_store_backup_unreadable",
                path=str(backup),
                error=repr(exc),
            )
        else:
            audit(
                "download_store_recovered",
                path=str(primary),
                backup_path=str(backup),
            )
            save_locked(store)
            return store

    raise RuntimeError(
        "Download store is unreadable and no valid backup is available; refusing to reset it"
    ) from primary_error


@contextmanager
def transaction() -> Generator[dict[str, Any]]:
    """Serialize one download-store transaction across threads and processes."""
    with STORE_LOCK, private_file_lock(lock_path()):
        yield load_locked()


def snapshot_path(link: dict[str, Any]) -> Path | None:
    """Resolve a validated opaque snapshot name inside the private directory."""
    name = str(link.get("snapshot_name") or "")
    if (
        not name
        or Path(name).name != name
        or not name.endswith(SNAPSHOT_SUFFIX)
    ):
        return None
    return snapshot_directory() / name


def remove_snapshot(link: dict[str, Any]) -> None:
    """Delete one registered snapshot when it is no longer reachable."""
    path = snapshot_path(link)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def prune_locked(
    store: dict[str, Any],
    now: float,
    *,
    exclude_token: str | None = None,
) -> bool:
    """Remove expired, exhausted, malformed, and orphaned snapshots."""
    links = store.get("links", {})
    changed = False
    for token, link in list(links.items()):
        if token == exclude_token:
            continue
        snapshot = snapshot_path(link)
        expires_at = float(link.get("expires_at", 0))
        maximum = int(link.get("max_downloads", 0))
        downloads = int(link.get("downloads", 0))
        if (
            snapshot is None
            or expires_at <= now
            or (maximum > 0 and downloads >= maximum)
        ):
            remove_snapshot(link)
            links.pop(token, None)
            changed = True

    referenced = {
        str(link.get("snapshot_name") or "")
        for link in links.values()
        if isinstance(link, dict)
    }
    directory = snapshot_directory()
    for path in directory.glob(f"*{SNAPSHOT_SUFFIX}"):
        if path.name in referenced:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        changed = True

    prune_before = time.time() - STAGING_PRUNE_GRACE_S
    for path in directory.glob(f".*{STAGING_SUFFIX}"):
        try:
            if path.stat().st_mtime > prune_before:
                continue
            path.unlink(missing_ok=True)
        except OSError:
            continue
        changed = True
    return changed


def open_snapshot(link: dict[str, Any]) -> tuple[BinaryIO, Path]:
    """Open a registered snapshot and verify its stored file identity."""
    path = snapshot_path(link)
    if path is None:
        raise FileNotFoundError("download snapshot metadata is invalid")
    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    handle: BinaryIO | None = None
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("download snapshot is not a regular file")
        if (
            int(link.get("snapshot_device", -1)) != int(file_stat.st_dev)
            or int(link.get("snapshot_inode", -1)) != int(file_stat.st_ino)
            or int(link.get("bytes", -1)) != int(file_stat.st_size)
        ):
            raise ValueError("download snapshot identity changed")
        handle = os.fdopen(descriptor, "rb")
        digest = hashlib.sha256()
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != str(link.get("sha256") or ""):
            raise ValueError("download snapshot digest changed")
        handle.seek(0)
        return handle, path
    except Exception:
        if handle is not None:
            handle.close()
        else:
            os.close(descriptor)
        raise


def release_claim(claim: ClaimedDownload) -> None:
    """Close a claim and remove its snapshot after the final allowed GET."""
    with contextlib.suppress(OSError):
        claim.handle.close()
    if claim.remove_snapshot_after:
        with contextlib.suppress(OSError):
            claim.path.unlink(missing_ok=True)
