"""Durable private management state for public file links."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from ..audit import audit
from ..persistence import get_state_store
from ..utils.private_files import atomic_write_private_text, private_file_lock
from .payload_store import PayloadStore

STORE_VERSION = 3
LEGACY_STORE_VERSIONS = frozenset({1, 2})
STORE_FILE_NAME = "downloads.json"
BACKUP_FILE_NAME = "downloads.json.bak"
LOCK_FILE_NAME = "downloads.lock"
STORE_LOCK = threading.RLock()
_LEGACY_SNAPSHOT_SUFFIX = ".bin"


@dataclass(frozen=True)
class ClaimedDownload:
    """One validated open immutable payload claimed for an HTTP response."""

    handle: BinaryIO
    """Already-open private payload handle used for streaming."""

    path: Path
    """Opaque private payload path used for final cleanup."""

    link: dict[str, Any]
    """Validated metadata copied from the durable link store."""

    remove_snapshot_after: bool
    """Whether response completion should delete the payload bytes."""


@dataclass(frozen=True)
class _LoadedStore:
    store: dict[str, Any]
    migrated: bool = False


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


def _load_file(path: Path) -> _LoadedStore:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unsupported or invalid download store: {path}")
    version = data.get("version")
    if version not in {STORE_VERSION, *LEGACY_STORE_VERSIONS}:
        raise ValueError(f"unsupported or invalid download store: {path}")
    links = data.get("links")
    if not isinstance(links, dict):
        raise ValueError(f"download store links field is invalid: {path}")
    if version in LEGACY_STORE_VERSIONS:
        audit(
            "download_store_migrated",
            path=str(path),
            from_version=version,
            to_version=STORE_VERSION,
            migrated_links=0,
            dropped_links=len(links),
        )
        return _LoadedStore(empty_store(), migrated=True)
    normalized = {
        str(link_id): link
        for link_id, link in links.items()
        if isinstance(link_id, str) and isinstance(link, dict)
    }
    return _LoadedStore({"version": STORE_VERSION, "links": normalized})


def save_locked(store: dict[str, Any]) -> None:
    """Atomically persist primary state without retaining a stale recovery copy."""
    store["version"] = STORE_VERSION
    payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        _remove_stale_backup()
    except OSError as exc:
        audit(
            "download_store_backup_invalidation_failed",
            path=str(backup_path()),
            error=repr(exc),
        )
        raise RuntimeError(
            "Download store backup could not be invalidated before save"
        ) from exc
    atomic_write_private_text(store_path(), payload)
    try:
        atomic_write_private_text(backup_path(), payload)
    except OSError as exc:
        audit(
            "download_store_backup_write_failed",
            path=str(backup_path()),
            error=repr(exc),
        )


def _restore_primary_from_backup(store: dict[str, Any]) -> None:
    """Restore the primary from an already-validated current backup.

    The recovery copy is intentionally left untouched until the primary write
    succeeds.  If restoring the primary fails, the sole known-good backup must
    remain available for a later retry.
    """
    store["version"] = STORE_VERSION
    payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_private_text(store_path(), payload)


def _remove_stale_backup() -> None:
    """Remove a stale backup after a failed refresh."""
    backup_path().unlink(missing_ok=True)


def _backup_contains_legacy_bearers() -> bool:
    """Return whether the recovery copy is still a plaintext-token store."""
    backup = backup_path()
    if not backup.exists():
        return False
    try:
        data = json.loads(backup.read_text(encoding="utf-8"))
    except OSError, ValueError, json.JSONDecodeError:
        return False
    return (
        isinstance(data, dict) and data.get("version") in LEGACY_STORE_VERSIONS
    )


def _prune_legacy_snapshot_directory() -> None:
    """Remove v2 snapshot artifacts after no durable store can reference them."""
    directory = get_state_store().layout.download_snapshots_dir
    if not directory.exists():
        return
    for pattern in (f"*{_LEGACY_SNAPSHOT_SUFFIX}", ".*.tmp"):
        for path in directory.glob(pattern):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)


def _finish_migration(loaded: _LoadedStore) -> dict[str, Any]:
    if not loaded.migrated:
        return loaded.store
    save_locked(loaded.store)
    _prune_legacy_snapshot_directory()
    return loaded.store


def load_locked() -> dict[str, Any]:
    """Load primary state or recover from backup without silent reset."""
    primary = store_path()
    backup = backup_path()
    if not primary.exists() and not backup.exists():
        return empty_store()

    primary_error: Exception | None = None
    if primary.exists():
        try:
            loaded = _load_file(primary)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            primary_error = exc
            audit(
                "download_store_unreadable", path=str(primary), error=repr(exc)
            )
        else:
            current = _finish_migration(loaded)
            if not loaded.migrated and _backup_contains_legacy_bearers():
                # A previous migration may have committed v3 primary metadata
                # before failing to scrub a plaintext-token backup.  Refuse to
                # continue until the stale bearer-bearing recovery copy is
                # refreshed or removed.
                save_locked(current)
                _prune_legacy_snapshot_directory()
            return current

    if backup.exists():
        try:
            loaded = _load_file(backup)
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
            if loaded.migrated:
                return _finish_migration(loaded)
            _restore_primary_from_backup(loaded.store)
            return loaded.store

    raise RuntimeError(
        "Download store is unreadable and no valid backup is available; refusing to reset it"
    ) from primary_error


@contextmanager
def transaction() -> Generator[dict[str, Any]]:
    """Serialize one download-store transaction across threads and processes."""
    with STORE_LOCK, private_file_lock(lock_path()):
        yield load_locked()


def prune_locked(
    store: dict[str, Any],
    now: float,
    *,
    payloads: PayloadStore,
    exclude_link_id: str | None = None,
) -> bool:
    """Remove expired, exhausted, and malformed file-link resources."""
    links = store.get("links", {})
    changed = False
    for link_id, link in list(links.items()):
        if link_id == exclude_link_id:
            continue
        payload_id = str(link.get("payload_id") or "")
        try:
            payloads.path(payload_id, namespace="download")
            valid_payload_id = True
        except ValueError:
            valid_payload_id = False
        expires_at = float(link.get("expires_at", 0))
        maximum = int(link.get("max_downloads", 0))
        downloads = int(link.get("downloads", 0))
        if (
            not valid_payload_id
            or expires_at <= now
            or (maximum > 0 and downloads >= maximum)
        ):
            links.pop(link_id, None)
            changed = True
    if payloads.prune_staging_files("download"):
        changed = True
    return changed


def cleanup_unreferenced_payloads_locked(
    store: dict[str, Any], *, payloads: PayloadStore
) -> bool:
    """Remove committed download payloads not referenced by durable metadata.

    Callers must invoke this only against metadata that is already durable.  In
    particular, metadata removals are persisted before this cleanup runs so a
    failed state write can never turn a valid durable link into a missing
    payload.
    """
    links = store.get("links", {})
    referenced_payload_ids = {
        str(link.get("payload_id") or "")
        for link in links.values()
        if isinstance(link, dict)
    }
    try:
        return payloads.prune_unreferenced_payloads(
            "download", referenced_payload_ids
        )
    except OSError as exc:
        audit("download_payload_cleanup_failed", error=repr(exc))
        return False


def open_snapshot(
    link: dict[str, Any], *, payloads: PayloadStore
) -> tuple[BinaryIO, Path]:
    """Open and verify the immutable payload referenced by one link."""
    return payloads.open_payload(
        str(link.get("payload_id") or ""),
        namespace="download",
        size=int(link.get("bytes", -1)),
        sha256=str(link.get("sha256") or ""),
    )


def release_claim(claim: ClaimedDownload) -> None:
    """Close a claim and remove its payload after the final allowed GET."""
    with contextlib.suppress(OSError):
        claim.handle.close()
    if claim.remove_snapshot_after:
        with contextlib.suppress(OSError):
            claim.path.unlink(missing_ok=True)
