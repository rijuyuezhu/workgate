"""Durable private management state for public file links."""

import contextlib
import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from ..audit import audit
from ..persistence import get_state_store
from ..protocol.ids import LinkId, PayloadId, SessionId
from ..utils.private_files import atomic_write_private_text, private_file_lock
from .payload_store import PayloadStore

STORE_VERSION = 3
LEGACY_STORE_VERSIONS = frozenset({1, 2})
STORE_FILE_NAME = "downloads.json"
BACKUP_FILE_NAME = "downloads.json.bak"
LOCK_FILE_NAME = "downloads.lock"
STORE_LOCK = threading.RLock()
_LEGACY_SNAPSHOT_SUFFIX = ".bin"
_Sha256 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
]
_TokenFingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{16}$", min_length=16, max_length=16),
]
_NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
_NonNegativeInt = Annotated[int, Field(ge=0)]
_NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class _DurableFileLink(BaseModel):
    """Strict current-version durable metadata for one public file link."""

    model_config = ConfigDict(strict=True, extra="forbid")

    link_id: LinkId
    token_sha256: _Sha256
    token_fingerprint: _TokenFingerprint
    payload_id: PayloadId
    display_path: str | None
    filename: _NonEmptyString
    inline: bool
    media_type: _NonEmptyString
    bytes: _NonNegativeInt
    sha256: _Sha256
    created_at: _NonNegativeFloat
    expires_at: _NonNegativeFloat
    downloads: _NonNegativeInt
    max_downloads: _NonNegativeInt
    session_id: SessionId | None
    last_download_at: _NonNegativeFloat | None = None

    @model_validator(mode="after")
    def validate_relationships(self) -> _DurableFileLink:
        if self.token_fingerprint != self.token_sha256[:16]:
            raise ValueError("token_fingerprint does not match token_sha256")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.max_downloads > 0 and self.downloads > self.max_downloads:
            raise ValueError("downloads exceeds max_downloads")
        return self


class _DurableDownloadStore(BaseModel):
    """Strict current-version durable public-link registry."""

    model_config = ConfigDict(strict=True, extra="forbid")

    version: Literal[3]
    links: dict[LinkId, _DurableFileLink]

    @model_validator(mode="after")
    def validate_link_keys(self) -> _DurableDownloadStore:
        if any(link_id != link.link_id for link_id, link in self.links.items()):
            raise ValueError("link management identity does not match its key")
        return self


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


def _invalid_store(path: Path, detail: str) -> ValueError:
    return ValueError(f"invalid download store {path}: {detail}")


def _validate_current_store(data: object, path: Path) -> dict[str, Any]:
    try:
        _DurableDownloadStore.model_validate(data)
    except ValidationError:
        raise _invalid_store(
            path, "current-version structure is invalid"
        ) from None
    assert isinstance(data, dict)
    links = data["links"]
    assert isinstance(links, dict)
    return {"version": STORE_VERSION, "links": dict(links)}


def _serialize_current_store(store: dict[str, Any]) -> str:
    store["version"] = STORE_VERSION
    validated = _validate_current_store(store, store_path())
    return json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True)


def _load_file(path: Path, *, audit_legacy: bool = True) -> _LoadedStore:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unsupported or invalid download store: {path}")
    version = data.get("version")
    if type(version) is not int or version not in {
        STORE_VERSION,
        *LEGACY_STORE_VERSIONS,
    }:
        raise ValueError(f"unsupported or invalid download store: {path}")
    links = data.get("links")
    if not isinstance(links, dict):
        raise ValueError(f"download store links field is invalid: {path}")
    if version in LEGACY_STORE_VERSIONS:
        if audit_legacy:
            audit(
                "download_store_migrated",
                path=str(path),
                from_version=version,
                to_version=STORE_VERSION,
                migrated_links=0,
                dropped_links=len(links),
            )
        return _LoadedStore(empty_store(), migrated=True)
    return _LoadedStore(_validate_current_store(data, path))


def save_locked(store: dict[str, Any]) -> None:
    """Atomically persist primary state without retaining a stale recovery copy."""
    payload = _serialize_current_store(store)
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
    payload = _serialize_current_store(store)
    atomic_write_private_text(store_path(), payload)


def _remove_stale_backup() -> None:
    """Remove a stale backup after a failed refresh."""
    backup_path().unlink(missing_ok=True)


def _backup_matches_current_store(store: dict[str, Any]) -> bool:
    """Return whether an existing recovery copy is a validated exact v3 copy."""
    backup = backup_path()
    if not backup.exists():
        return True
    try:
        loaded = _load_file(backup, audit_legacy=False)
    except OSError, ValueError, json.JSONDecodeError:
        return False
    return not loaded.migrated and loaded.store == store


def _refresh_unsafe_backup(store: dict[str, Any]) -> None:
    """Scrub one stale/invalid backup and best-effort replace it from healthy v3 state."""
    payload = _serialize_current_store(store)
    try:
        _remove_stale_backup()
    except OSError as exc:
        audit(
            "download_store_backup_invalidation_failed",
            path=str(backup_path()),
            error=repr(exc),
        )
        raise RuntimeError(
            "Download store backup could not be invalidated before refresh"
        ) from exc
    try:
        atomic_write_private_text(backup_path(), payload)
    except OSError as exc:
        audit(
            "download_store_backup_write_failed",
            path=str(backup_path()),
            error=repr(exc),
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
            if not loaded.migrated and not _backup_matches_current_store(
                current
            ):
                # A healthy current primary must never coexist with an unsafe
                # recovery copy.  Legacy, malformed, unsupported, or merely
                # stale-but-valid v3 backups are scrubbed before continuing.
                _refresh_unsafe_backup(current)
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
