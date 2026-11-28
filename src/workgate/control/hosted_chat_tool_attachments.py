"""Explicit private attachment publication through native Session transfer."""

import hashlib
import mimetypes
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from ..hosted_chat.attachment_uploads import (
    MAX_ATTACHMENT_BYTES,
    publish_attachment,
)
from ..hosted_chat.content_filters import (
    filtered_tool_output,
    filters_from_snapshot,
)
from ..hosted_chat.tool_attachment_publication import (
    TOOL_NAME,
    ToolAttachmentPublication,
)
from ..protocol.ids import SessionId
from ..protocol.transfer import DEFAULT_TRANSFER_CHUNK_BYTES
from .downloads import _safe_download_filename
from .hosted_chat_native_dispatch import _envelope
from .runtime import ControlRuntime


class PublishAttachmentRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: SessionId
    path: str = Field(
        min_length=1,
        max_length=4096,
        description="Exact file path in the owned execution Session. Relative paths use its working directory.",
    )


def attachment_definition() -> dict:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Publish one explicitly selected file from an owned native Session as a private downloadable attachment in this conversation. No public link is created. Maximum 32 MiB.",
            "parameters": PublishAttachmentRequest.model_json_schema(),
        },
    }


def publication_result(record: dict) -> dict:
    """Only stable public fields become the native tool receipt."""
    return _envelope(
        {
            key: record[key]
            for key in ("file_id", "filename", "bytes", "type", "filepath")
        }
    )


async def publish_native_attachment(
    runtime: ControlRuntime,
    binder: ToolAttachmentPublication,
    request: PublishAttachmentRequest,
    *,
    check_grant: Callable[[], None],
) -> dict:
    prior = binder.lookup()
    if prior is not None:
        return publication_result(prior)
    snapshot = await runtime.download_service.export_private_snapshot(
        session_id=request.session_id,
        path=request.path,
        maximum=MAX_ATTACHMENT_BYTES,
        check_grant=check_grant,
    )
    try:
        name = _safe_download_filename(None, snapshot.source_name)[:200]
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        digest = hashlib.sha256()

        async def chunks():
            with snapshot.staging_path.open("rb") as handle:
                while chunk := handle.read(DEFAULT_TRANSFER_CHUNK_BYTES):
                    check_grant()
                    digest.update(chunk)
                    yield chunk
            if digest.hexdigest() != snapshot.sha256:
                raise OSError(
                    "Private native snapshot changed before publication"
                )

        def bind(db, ident):
            record = binder.bind(db, ident)
            filters = filters_from_snapshot(db, binder.lease.owner)
            if isinstance(
                filtered_tool_output(TOOL_NAME, record, filters), str
            ):
                raise PermissionError(
                    "Tool attachment metadata blocked by content policy"
                )

        await publish_attachment(
            binder.lease.database,
            binder.lease.owner,
            name,
            mime,
            chunks(),
            expected_size=snapshot.size,
            check_grant=check_grant,
            bind=bind,
        )
        record = binder.lookup()
        if record is None:
            raise RuntimeError(
                "Native attachment publication receipt is missing"
            )
        return publication_result(record)
    finally:
        snapshot.staging_path.unlink(missing_ok=True)
