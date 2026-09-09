"""Control-owned public file-link snapshots sourced through executor RPC."""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import mimetypes
import os
import secrets
import time
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from ..audit import audit
from ..config.settings import get_settings
from ..errors import exception_from_tool_error
from ..protocol.transfer import DEFAULT_TRANSFER_CHUNK_BYTES
from ..schemas.result_models.downloads import (
    CreateFileLinkOutput,
    FileLinkSummary,
    ListFileLinksOutput,
    RevokeFileLinkOutput,
)
from ..schemas.result_models.transfer import (
    TransferReadChunkOutput,
    TransferStatOutput,
)
from .download_snapshot import (
    DownloadSnapshot,
    assert_shareable_size,
    new_snapshot_path,
    new_staging_path,
    open_private_staging,
)
from .download_store import (
    ClaimedDownload,
    open_snapshot,
    prune_locked,
    remove_snapshot,
    save_locked,
    transaction,
)
from .executor_transport import ExecutorTransport
from .sessions import ControlSessionCoordinator
from .state import ControlSessionRecord

DOWNLOAD_PREFIX = "/download"


def download_token_fingerprint(token: str) -> str:
    """Return a short non-secret fingerprint for a download token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _now_s() -> float:
    return time.time()


def _coerce_download_ttl(ttl_s: int | None) -> int:
    settings = get_settings()
    requested = (
        settings.file_download_default_ttl_s if ttl_s is None else int(ttl_s)
    )
    if requested <= 0:
        raise ValueError("ttl_s must be positive")
    return min(requested, settings.file_download_max_ttl_s)


def _coerce_max_downloads(max_downloads: int | None) -> int:
    settings = get_settings()
    requested = (
        settings.file_download_default_max_downloads
        if max_downloads is None
        else int(max_downloads)
    )
    if requested < 0:
        raise ValueError("max_downloads must be >= 0; use 0 for unlimited")
    return requested


def _safe_download_filename(filename: str | None, source: str | Path) -> str:
    source_name = Path(source).name
    candidate = str(filename) if filename else source_name
    candidate = candidate.replace("/", "_").replace("\\", "_")
    candidate = "".join(
        character
        for character in candidate.strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return candidate[:255] or "download"


def _infer_download_media_type(source_name: str, filename: str) -> str:
    return (
        mimetypes.guess_type(source_name)[0]
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )


def _download_link_summary(token: str, link: dict[str, Any]) -> FileLinkSummary:
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
        ttl_remaining_s=max(0, int(link.get("expires_at", 0) - _now_s())),
        downloads=link.get("downloads", 0),
        max_downloads=link.get("max_downloads", 0),
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
    created_at = _now_s()
    browser_filename = _safe_download_filename(filename, snapshot.source_name)
    media_type = _infer_download_media_type(
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
                "expires_at": created_at + _coerce_download_ttl(ttl_s),
                "downloads": 0,
                "max_downloads": _coerce_max_downloads(max_downloads),
                "session_id": session_id,
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
    )
    return CreateFileLinkOutput(
        **_download_link_summary(token, link).model_dump()
    )


def _list_file_links_owned(
    include_expired: bool = False, session_id: str | None = None
) -> ListFileLinksOutput:
    with transaction() as store:
        changed = False
        if not include_expired:
            changed = prune_locked(store, _now_s())
        links = [
            _download_link_summary(token, link)
            for token, link in store.get("links", {}).items()
            if session_id is None or link.get("session_id") == session_id
        ]
        if changed:
            save_locked(store)
    links.sort(key=lambda item: item.created_at or 0, reverse=True)
    return ListFileLinksOutput(links=links)


def _revoke_file_link_owned(
    token: str, session_id: str | None = None
) -> RevokeFileLinkOutput:
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


def claim_download(
    token: str, *, consume: bool
) -> ClaimedDownload | dict[str, Any]:
    """Claim one validated control-owned snapshot for HTTP serving."""
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
                if prune_locked(store, _now_s()):
                    save_locked(store)
                return {
                    "status_code": 404,
                    "error": "download_not_found",
                    "message": "Link not found",
                }
            current = _now_s()
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


class ControlDownloadService:
    """Own public bearer-link state while executors remain filesystem authority."""

    def __init__(
        self,
        sessions: ControlSessionCoordinator,
        transport: ExecutorTransport,
    ) -> None:
        self._sessions = sessions
        self._transport = transport

    async def create(
        self,
        *,
        session_id: str,
        path: str,
        ttl_s: int | None,
        filename: str | None,
        max_downloads: int | None,
        inline: bool,
    ) -> CreateFileLinkOutput:
        """Export one immutable executor file into control-owned share storage."""
        async with self._sessions.session_admission((session_id,)) as records:
            record = records[0]
            snapshot = await self._export_snapshot(record, path)
            return _register_snapshot(
                snapshot,
                ttl_s=ttl_s,
                filename=filename,
                max_downloads=max_downloads,
                inline=inline,
                session_id=session_id,
            )

    async def list(
        self, *, session_id: str, include_expired: bool
    ) -> ListFileLinksOutput:
        """List control-owned links for one still-active shared session."""
        async with self._sessions.session_admission((session_id,)):
            return _list_file_links_owned(include_expired, session_id)

    async def revoke(
        self, *, session_id: str, token: str
    ) -> RevokeFileLinkOutput:
        """Revoke one control-owned bearer link for an active shared session."""
        async with self._sessions.session_admission((session_id,)):
            return _revoke_file_link_owned(token, session_id)

    async def _export_snapshot(
        self, record: ControlSessionRecord, path: str
    ) -> DownloadSnapshot:
        stat = TransferStatOutput.model_validate(
            await self._call(
                record, "transfer_stat", {"path": path, "sha256": True}
            )
        )
        if stat.type != "file" or stat.size is None or stat.sha256 is None:
            raise ValueError(f"Not a regular file: {path}")
        expected_size = int(stat.size)
        assert_shareable_size(expected_size)

        staging = new_staging_path()
        digest = hashlib.sha256()
        offset = 0
        try:
            with open_private_staging(staging) as destination:
                while offset < expected_size:
                    chunk = TransferReadChunkOutput.model_validate(
                        await self._call(
                            record,
                            "transfer_read_chunk",
                            {
                                "path": path,
                                "offset": offset,
                                "chunk_size": min(
                                    DEFAULT_TRANSFER_CHUNK_BYTES,
                                    expected_size - offset,
                                ),
                            },
                        )
                    )
                    decoded = self._decode_chunk(
                        chunk,
                        expected_offset=offset,
                        expected_size=expected_size,
                        expected_sha256=stat.sha256,
                    )
                    if not decoded:
                        raise RuntimeError(
                            "Executor snapshot transfer ended before completion"
                        )
                    destination.write(decoded)
                    digest.update(decoded)
                    offset += len(decoded)
                    if chunk.eof and offset != expected_size:
                        raise RuntimeError(
                            "Executor snapshot reported EOF before completion"
                        )
                    if not chunk.eof and offset >= expected_size:
                        raise RuntimeError(
                            "Executor snapshot omitted EOF at the expected size"
                        )
                destination.flush()
            actual_sha256 = digest.hexdigest()
            if offset != expected_size:
                raise RuntimeError("Executor snapshot transfer size mismatch")
            if actual_sha256 != stat.sha256:
                raise RuntimeError(
                    "Executor file changed while creating snapshot"
                )
            source_name = Path(stat.path or path).name or Path(path).name
            return DownloadSnapshot(
                staging_path=staging,
                display_path=stat.path,
                source_name=source_name,
                size=expected_size,
                sha256=actual_sha256,
            )
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    async def _call(
        self,
        record: ControlSessionRecord,
        op: str,
        args: dict[str, JsonValue],
    ) -> JsonValue:
        result = await self._transport.call(
            str(record.executor_id),
            op,
            args,
            session_id=str(record.session_id),
        )
        if result.ok:
            self._sessions.observe_session_activity(str(record.session_id))
            return result.result
        await self._sessions.reconcile_session_activity_after_error(record)
        assert result.error is not None
        if result.error.data is not None:
            raise exception_from_tool_error(dict(result.error.data))
        raise RuntimeError(
            f"executor {op} failed: {result.error.code}: {result.error.message}"
        )

    @staticmethod
    def _decode_chunk(
        chunk: TransferReadChunkOutput,
        *,
        expected_offset: int,
        expected_size: int,
        expected_sha256: str,
    ) -> bytes:
        if chunk.offset != expected_offset or chunk.size != expected_size:
            raise RuntimeError("Executor file changed while creating snapshot")
        if chunk.sha256 != expected_sha256:
            raise RuntimeError("Executor file changed while creating snapshot")
        try:
            decoded = base64.b64decode(
                chunk.data_b64.encode("ascii"), validate=True
            )
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise RuntimeError(
                "Executor snapshot chunk is not valid base64"
            ) from exc
        if len(decoded) != chunk.bytes:
            raise RuntimeError("Executor snapshot chunk length mismatch")
        return decoded
