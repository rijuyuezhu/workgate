"""Control-owned public file-link snapshots sourced through executor RPC."""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from ..audit import audit
from ..config.control import ControlSettingsView
from ..config.settings import get_settings
from ..errors import exception_from_tool_error
from ..protocol.ids import new_link_id
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
    new_staging_path,
    open_private_staging,
)
from .download_store import (
    ClaimedDownload,
    cleanup_unreferenced_payloads_locked,
    open_snapshot,
    prune_locked,
    save_locked,
    transaction,
)
from .executor_transport import ExecutorTransport
from .payload_store import PayloadStore
from .sessions import ControlSessionCoordinator
from .state import ControlSessionRecord

DOWNLOAD_PREFIX = "/download"


def download_token_fingerprint(token: str) -> str:
    """Return a short non-secret fingerprint for a download token."""
    return download_token_sha256(token)[:16]


def download_token_sha256(token: str) -> str:
    """Return the full private lookup digest for a bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_s() -> float:
    return time.time()


def _active_settings(
    settings: ControlSettingsView | None,
) -> ControlSettingsView:
    """Use explicit control authority, with ambient settings only for compatibility callers."""
    return get_settings() if settings is None else settings


def _payload_store(settings: ControlSettingsView | None) -> PayloadStore:
    return PayloadStore(_active_settings(settings).data_dir)


def _coerce_download_ttl(
    ttl_s: int | None, settings: ControlSettingsView | None = None
) -> int:
    active = _active_settings(settings)
    requested = (
        active.file_download_default_ttl_s if ttl_s is None else int(ttl_s)
    )
    if requested <= 0:
        raise ValueError("ttl_s must be positive")
    return min(requested, active.file_download_max_ttl_s)


def _coerce_max_downloads(
    max_downloads: int | None, settings: ControlSettingsView | None = None
) -> int:
    active = _active_settings(settings)
    requested = (
        active.file_download_default_max_downloads
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


def _download_link_summary(
    link_id: str, link: dict[str, Any]
) -> FileLinkSummary:
    return FileLinkSummary(
        link_id=link_id,
        token_fingerprint=str(link.get("token_fingerprint") or ""),
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
    settings: ControlSettingsView | None = None,
) -> CreateFileLinkOutput:
    active = _active_settings(settings)
    payloads = PayloadStore(active.data_dir)
    token = secrets.token_urlsafe(32)
    token_sha256 = download_token_sha256(token)
    link_id = str(new_link_id())
    created_at = _now_s()
    browser_filename = _safe_download_filename(filename, snapshot.source_name)
    media_type = _infer_download_media_type(
        snapshot.source_name, browser_filename
    )
    payload_id: str | None = None
    link: dict[str, Any] | None = None
    try:
        with transaction() as store:
            cleanup_unreferenced_payloads_locked(store, payloads=payloads)
            prune_locked(store, created_at, payloads=payloads)
            payload = payloads.commit_staging(
                snapshot.staging_path,
                namespace="download",
                size=snapshot.size,
                sha256=snapshot.sha256,
            )
            payload_id = payload.payload_id
            link = {
                "link_id": link_id,
                "token_sha256": token_sha256,
                "token_fingerprint": token_sha256[:16],
                "payload_id": payload.payload_id,
                "display_path": snapshot.display_path,
                "filename": browser_filename,
                "inline": bool(inline),
                "media_type": media_type,
                "bytes": snapshot.size,
                "sha256": snapshot.sha256,
                "created_at": created_at,
                "expires_at": created_at + _coerce_download_ttl(ttl_s, active),
                "downloads": 0,
                "max_downloads": _coerce_max_downloads(max_downloads, active),
                "session_id": session_id,
            }
            store["links"][link_id] = link
            save_locked(store)
            cleanup_unreferenced_payloads_locked(store, payloads=payloads)
    except Exception:
        if payload_id is not None:
            payloads.remove_payload(payload_id, namespace="download")
        raise
    finally:
        snapshot.staging_path.unlink(missing_ok=True)
    assert link is not None
    audit(
        "download_link_created",
        link_id=link_id,
        payload_id=payload_id,
        path=link["display_path"],
        token_fingerprint=token_sha256[:16],
        expires_at=link["expires_at"],
        inline=link["inline"],
    )
    return CreateFileLinkOutput(
        **_download_link_summary(link_id, link).model_dump(),
        token=token,
        url=f"{active.resolved_base_url}{DOWNLOAD_PREFIX}/{token}",
    )


def _list_file_links_owned(
    include_expired: bool = False,
    session_id: str | None = None,
    *,
    settings: ControlSettingsView | None = None,
) -> ListFileLinksOutput:
    payloads = _payload_store(settings)
    with transaction() as store:
        cleanup_unreferenced_payloads_locked(store, payloads=payloads)
        changed = False
        if not include_expired:
            changed = prune_locked(store, _now_s(), payloads=payloads)
        links = [
            _download_link_summary(link_id, link)
            for link_id, link in store.get("links", {}).items()
            if session_id is None or link.get("session_id") == session_id
        ]
        if changed:
            save_locked(store)
            cleanup_unreferenced_payloads_locked(store, payloads=payloads)
    links.sort(key=lambda item: item.created_at or 0, reverse=True)
    return ListFileLinksOutput(links=links)


def _revoke_file_link_owned(
    link_id: str,
    session_id: str | None = None,
    *,
    settings: ControlSettingsView | None = None,
) -> RevokeFileLinkOutput:
    payloads = _payload_store(settings)
    removed: dict[str, Any] | None = None
    with transaction() as store:
        cleanup_unreferenced_payloads_locked(store, payloads=payloads)
        link = store.get("links", {}).get(link_id)
        if link is not None and (
            session_id is None or link.get("session_id") == session_id
        ):
            removed = store["links"].pop(link_id, None)
        if removed is not None:
            save_locked(store)
            cleanup_unreferenced_payloads_locked(store, payloads=payloads)
    if removed is not None:
        audit(
            "download_link_revoked",
            link_id=link_id,
            payload_id=removed.get("payload_id"),
            path=removed.get("display_path"),
            token_fingerprint=removed.get("token_fingerprint"),
        )
    return RevokeFileLinkOutput(revoked=removed is not None, link_id=link_id)


def _link_for_token(
    links: dict[str, Any], token: str
) -> tuple[str, dict[str, Any]] | None:
    candidate = download_token_sha256(token)
    for link_id, raw_link in links.items():
        if not isinstance(raw_link, dict):
            continue
        stored = str(raw_link.get("token_sha256") or "")
        if len(stored) == len(candidate) and secrets.compare_digest(
            stored, candidate
        ):
            return str(link_id), raw_link
    return None


def claim_download(
    token: str,
    *,
    consume: bool,
    settings: ControlSettingsView | None = None,
) -> ClaimedDownload | dict[str, Any]:
    """Claim one validated control-owned snapshot for HTTP serving."""
    active = _active_settings(settings)
    payloads = PayloadStore(active.data_dir)
    if not active.file_download_enabled:
        return {
            "status_code": 404,
            "error": "download_disabled",
            "message": "File downloads are disabled",
        }
    handle = None
    try:
        with transaction() as store:
            cleanup_unreferenced_payloads_locked(store, payloads=payloads)
            links = store.get("links", {})
            resolved = _link_for_token(links, token)
            if resolved is None:
                if prune_locked(store, _now_s(), payloads=payloads):
                    save_locked(store)
                    cleanup_unreferenced_payloads_locked(
                        store, payloads=payloads
                    )
                return {
                    "status_code": 404,
                    "error": "download_not_found",
                    "message": "Link not found",
                }
            link_id, link = resolved
            current = _now_s()
            if float(link.get("expires_at", 0)) <= current:
                links.pop(link_id, None)
                save_locked(store)
                cleanup_unreferenced_payloads_locked(store, payloads=payloads)
                return {
                    "status_code": 410,
                    "error": "download_expired",
                    "message": "Link has expired",
                }
            maximum = int(link.get("max_downloads", 0))
            downloads = int(link.get("downloads", 0))
            if maximum > 0 and downloads >= maximum:
                links.pop(link_id, None)
                save_locked(store)
                cleanup_unreferenced_payloads_locked(store, payloads=payloads)
                return {
                    "status_code": 410,
                    "error": "download_exhausted",
                    "message": "Link has reached its use limit",
                }
            if (
                active.file_download_max_file_bytes > 0
                and int(link.get("bytes", 0))
                > active.file_download_max_file_bytes
            ):
                return {
                    "status_code": 403,
                    "error": "download_too_large",
                    "message": "The snapshot exceeds the configured size limit",
                }
            try:
                handle, path = open_snapshot(link, payloads=payloads)
            except FileNotFoundError, OSError, PermissionError, ValueError:
                links.pop(link_id, None)
                save_locked(store)
                cleanup_unreferenced_payloads_locked(store, payloads=payloads)
                return {
                    "status_code": 404,
                    "error": "download_missing",
                    "message": "The shared file snapshot is unavailable",
                }
            remove_after = False
            changed = False
            if consume:
                next_downloads = downloads + 1
                link["downloads"] = next_downloads
                link["last_download_at"] = current
                remove_after = maximum > 0 and next_downloads >= maximum
                changed = True
            if prune_locked(
                store,
                current,
                payloads=payloads,
                exclude_link_id=link_id,
            ):
                changed = True
            if changed:
                save_locked(store)
                cleanup_unreferenced_payloads_locked(store, payloads=payloads)
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
        settings: ControlSettingsView,
    ) -> None:
        self._sessions = sessions
        self._transport = transport
        self._settings = settings

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
                settings=self._settings,
            )

    async def list(
        self, *, session_id: str, include_expired: bool
    ) -> ListFileLinksOutput:
        """List control-owned links for one still-active shared session."""
        async with self._sessions.session_admission((session_id,)):
            return _list_file_links_owned(
                include_expired, session_id, settings=self._settings
            )

    async def revoke(
        self, *, session_id: str, link_id: str
    ) -> RevokeFileLinkOutput:
        """Revoke one control-owned bearer link for an active shared session."""
        async with self._sessions.session_admission((session_id,)):
            return _revoke_file_link_owned(
                link_id, session_id, settings=self._settings
            )

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
        assert_shareable_size(
            expected_size,
            maximum=self._settings.file_download_max_file_bytes,
        )

        staging = new_staging_path(data_dir=self._settings.data_dir)
        digest = hashlib.sha256()
        offset = 0
        try:
            with open_private_staging(
                staging, data_dir=self._settings.data_dir
            ) as destination:
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
