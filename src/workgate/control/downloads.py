"""Control-owned public file-link snapshots sourced through executor RPC."""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path

from pydantic import JsonValue

from ..errors import exception_from_tool_error
from ..ops.downloads import (
    _list_file_links_owned,
    _register_snapshot,
    _revoke_file_link_owned,
)
from ..ops.transfer import DEFAULT_TRANSFER_CHUNK_BYTES
from ..ops.utils.download_snapshot import (
    DownloadSnapshot,
    assert_shareable_size,
    new_staging_path,
    open_private_staging,
)
from ..schemas.result_models.downloads import (
    CreateFileLinkOutput,
    ListFileLinksOutput,
    RevokeFileLinkOutput,
)
from ..schemas.result_models.transfer import (
    TransferReadChunkOutput,
    TransferStatOutput,
)
from .executor_transport import ExecutorTransport
from .sessions import ControlSessionCoordinator
from .state import ControlSessionRecord


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
                # Compatibility projection only. The committed payload now lives on
                # control; executor/session topology is not inferred from this field.
                target="local",
                machine=None,
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
        self._sessions.observe_session_activity(str(record.session_id))
        if result.ok:
            return result.result
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
