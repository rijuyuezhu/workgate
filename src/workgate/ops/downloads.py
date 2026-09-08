"""Tokenized file download link state and policy operations."""

import asyncio
import contextlib
import hashlib
import mimetypes
import os
import secrets
import time
from pathlib import Path
from typing import Any

from ..audit import audit
from ..config.settings import get_settings
from ..schemas.result_models.downloads import (
    CreateFileLinkOutput,
    FileLinkSummary,
    ListFileLinksOutput,
    RevokeFileLinkOutput,
)
from ..tool_session.store import get_tool_session_store
from .utils.download_snapshot import (
    DownloadSnapshot,
    create_local_snapshot,
    create_remote_snapshot,
    new_snapshot_path,
)
from .utils.download_store import (
    ClaimedDownload,
    open_snapshot,
    prune_locked,
    remove_snapshot,
    save_locked,
    transaction,
)

DOWNLOAD_PREFIX = "/download"


def download_token_fingerprint(token: str) -> str:
    """Return a short non-secret fingerprint for a download token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def now_s() -> float:
    """Return the current Unix timestamp."""
    return time.time()


def coerce_download_ttl(ttl_s: int | None) -> int:
    """Clamp requested download-link lifetime to configured limits."""
    settings = get_settings()
    requested = (
        settings.file_download_default_ttl_s if ttl_s is None else int(ttl_s)
    )
    if requested <= 0:
        raise ValueError("ttl_s must be positive")
    return min(requested, settings.file_download_max_ttl_s)


def coerce_max_downloads(max_downloads: int | None) -> int:
    """Return a validated max-download count; 0 means unlimited."""
    settings = get_settings()
    requested = (
        settings.file_download_default_max_downloads
        if max_downloads is None
        else int(max_downloads)
    )
    if requested < 0:
        raise ValueError("max_downloads must be >= 0; use 0 for unlimited")
    return requested


def safe_download_filename(filename: str | None, source: str | Path) -> str:
    """Return a platform-independent, path-free browser filename."""
    source_name = Path(source).name
    candidate = str(filename) if filename else source_name
    candidate = candidate.replace("/", "_").replace("\\", "_")
    candidate = "".join(
        character
        for character in candidate.strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return candidate[:255] or "download"


def infer_download_media_type(source_name: str, filename: str) -> str:
    """Infer MIME from the source name, then the browser display name."""
    return (
        mimetypes.guess_type(source_name)[0]
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )


def download_link_summary(token: str, link: dict[str, Any]) -> FileLinkSummary:
    """Return the public summary for a stored download link."""
    return FileLinkSummary(
        token=token,
        url=f"{get_settings().resolved_base_url}{DOWNLOAD_PREFIX}/{token}",
        path=link.get("display_path"),
        filename=link.get("filename"),
        inline=bool(link.get("inline", False)),
        media_type=link.get("media_type"),
        bytes=link.get("bytes"),
        created_at=link.get("created_at"),
        expires_at=link.get("expires_at"),
        ttl_remaining_s=max(0, int(link.get("expires_at", 0) - now_s())),
        downloads=link.get("downloads", 0),
        max_downloads=link.get("max_downloads", 0),
        target=link.get("target", "local"),
        machine=link.get("machine"),
    )


def _register_snapshot(
    snapshot: DownloadSnapshot,
    *,
    ttl_s: int | None,
    filename: str | None,
    max_downloads: int | None,
    inline: bool,
    session_id: str | None,
) -> CreateFileLinkOutput:
    token = secrets.token_urlsafe(32)
    created_at = now_s()
    browser_filename = safe_download_filename(filename, snapshot.source_name)
    media_type = infer_download_media_type(
        snapshot.source_name, browser_filename
    )
    final_path = new_snapshot_path()
    link: dict[str, Any] | None = None
    try:
        with transaction() as store:
            prune_locked(store, created_at)
            os.replace(snapshot.staging_path, final_path)
            with contextlib.suppress(OSError):
                final_path.chmod(0o600)
            snapshot_stat = final_path.stat()
            link = {
                "display_path": snapshot.display_path,
                "filename": browser_filename,
                "inline": bool(inline),
                "media_type": media_type,
                "bytes": snapshot.size,
                "sha256": snapshot.sha256,
                "snapshot_name": final_path.name,
                "snapshot_device": int(snapshot_stat.st_dev),
                "snapshot_inode": int(snapshot_stat.st_ino),
                "created_at": created_at,
                "expires_at": created_at + coerce_download_ttl(ttl_s),
                "downloads": 0,
                "max_downloads": coerce_max_downloads(max_downloads),
                "session_id": session_id,
                "target": snapshot.target,
                "machine": snapshot.machine,
            }
            store["links"][token] = link
            save_locked(store)
    except Exception:
        final_path.unlink(missing_ok=True)
        raise
    finally:
        snapshot.staging_path.unlink(missing_ok=True)

    assert link is not None
    audit(
        "download_link_created",
        path=link["display_path"],
        token_sha256=download_token_fingerprint(token),
        expires_at=link["expires_at"],
        inline=link["inline"],
        target=link["target"],
        machine=link["machine"],
    )
    return CreateFileLinkOutput(
        **download_link_summary(token, link).model_dump()
    )


async def create_file_link_dispatch_execute(
    path: str,
    ttl_s: int | None = None,
    filename: str | None = None,
    max_downloads: int | None = None,
    session_id: str | None = None,
    inline: bool = False,
) -> CreateFileLinkOutput:
    """Create a local or remote session link backed by a local snapshot."""
    if not get_settings().file_download_enabled:
        raise PermissionError("file downloads are disabled")
    session = (
        get_tool_session_store().touch_session(session_id)
        if session_id is not None
        else None
    )
    if session is not None and session.target == "remote":
        snapshot = await create_remote_snapshot(path, session)
    else:
        snapshot = await asyncio.to_thread(create_local_snapshot, path, session)
    return await asyncio.to_thread(
        _register_snapshot,
        snapshot,
        ttl_s=ttl_s,
        filename=filename,
        max_downloads=max_downloads,
        inline=inline,
        session_id=session_id,
    )


def _list_file_links_owned(
    include_expired: bool = False, session_id: str | None = None
) -> ListFileLinksOutput:
    """List control-owned links without resolving machine/session state."""
    with transaction() as store:
        changed = False
        if not include_expired:
            changed = prune_locked(store, now_s())
        links = [
            download_link_summary(token, link)
            for token, link in store.get("links", {}).items()
            if session_id is None or link.get("session_id") == session_id
        ]
        if changed:
            save_locked(store)
    links.sort(key=lambda item: item.created_at or 0, reverse=True)
    return ListFileLinksOutput(links=links)


def list_file_links_execute(
    include_expired: bool = False, session_id: str | None = None
) -> ListFileLinksOutput:
    """List generated links owned by one explicit legacy tool session."""
    if session_id is not None:
        get_tool_session_store().touch_session(session_id)
    return _list_file_links_owned(include_expired, session_id)


def _revoke_file_link_owned(
    token: str, session_id: str | None = None
) -> RevokeFileLinkOutput:
    """Revoke one control-owned link without resolving machine/session state."""
    removed: dict[str, Any] | None = None
    with transaction() as store:
        link = store.get("links", {}).get(token)
        if link is not None and (
            session_id is None or link.get("session_id") == session_id
        ):
            removed = store["links"].pop(token, None)
        if removed is not None:
            remove_snapshot(removed)
            save_locked(store)
    if removed is not None:
        audit(
            "download_link_revoked",
            path=removed.get("display_path"),
            token_sha256=download_token_fingerprint(token),
        )
    return RevokeFileLinkOutput(revoked=removed is not None, token=token)


def revoke_file_link_execute(
    token: str, session_id: str | None = None
) -> RevokeFileLinkOutput:
    """Revoke one link owned by an explicit legacy tool session."""
    if session_id is not None:
        get_tool_session_store().touch_session(session_id)
    return _revoke_file_link_owned(token, session_id)


def claim_download(
    token: str, *, consume: bool
) -> ClaimedDownload | dict[str, Any]:
    """Claim a validated snapshot, or return an HTTP error payload."""
    settings = get_settings()
    if not settings.file_download_enabled:
        return {
            "status_code": 404,
            "error": "download_disabled",
            "message": "File downloads are disabled",
        }

    handle = None
    try:
        with transaction() as store:
            links = store.get("links", {})
            link = links.get(token)
            if not link:
                if prune_locked(store, now_s()):
                    save_locked(store)
                return {
                    "status_code": 404,
                    "error": "download_not_found",
                    "message": "Link not found",
                }

            current = now_s()
            if float(link.get("expires_at", 0)) <= current:
                remove_snapshot(link)
                links.pop(token, None)
                save_locked(store)
                return {
                    "status_code": 410,
                    "error": "download_expired",
                    "message": "Link has expired",
                }

            maximum = int(link.get("max_downloads", 0))
            downloads = int(link.get("downloads", 0))
            if maximum > 0 and downloads >= maximum:
                remove_snapshot(link)
                links.pop(token, None)
                save_locked(store)
                return {
                    "status_code": 410,
                    "error": "download_exhausted",
                    "message": "Link has reached its use limit",
                }

            if (
                settings.file_download_max_file_bytes > 0
                and int(link.get("bytes", 0))
                > settings.file_download_max_file_bytes
            ):
                return {
                    "status_code": 403,
                    "error": "download_too_large",
                    "message": "The snapshot exceeds the configured size limit",
                }

            try:
                handle, path = open_snapshot(link)
            except FileNotFoundError, OSError, PermissionError, ValueError:
                remove_snapshot(link)
                links.pop(token, None)
                save_locked(store)
                return {
                    "status_code": 404,
                    "error": "download_missing",
                    "message": "The shared file snapshot is unavailable",
                }

            remove_after = False
            if consume:
                next_downloads = downloads + 1
                link["downloads"] = next_downloads
                link["last_download_at"] = current
                remove_after = maximum > 0 and next_downloads >= maximum
                save_locked(store)
            if prune_locked(store, current, exclude_token=token):
                save_locked(store)
            return ClaimedDownload(
                handle=handle,
                path=path,
                link=dict(link),
                remove_snapshot_after=remove_after,
            )
    except (OSError, RuntimeError) as exc:
        if handle is not None:
            handle.close()
        audit("download_store_unavailable", error=repr(exc))
        return {
            "status_code": 500,
            "error": "download_store_unavailable",
            "message": "Download state is unavailable",
        }
