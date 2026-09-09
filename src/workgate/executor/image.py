"""Session-bound native image loading on one executor."""

import asyncio
import base64
from dataclasses import dataclass

from mcp.types import CallToolResult, ImageContent, TextContent

from ..schemas.result_models.image import ViewImageOutput
from ..tool_session.store import AgentSession, ToolSessionStore
from ..utils.image_types import detect_image_type
from .config import ExecutorConfig
from .path import relative_display_from_root


@dataclass(frozen=True)
class _ImageFile:
    path: str
    data: bytes
    format: str
    mime_type: str
    size: int


def _assert_image_size(size: int, maximum: int) -> None:
    """Reject empty or oversized native-image payloads."""
    maximum = max(1, int(maximum))
    if size <= 0:
        raise ValueError("Image file is empty")
    if size > maximum:
        raise ValueError(f"Refusing image of {size} bytes; max is {maximum}")


def _local_image(
    config: ExecutorConfig,
    store: ToolSessionStore,
    path: str,
    session: AgentSession,
) -> _ImageFile:
    """Read one bounded image from an executor-owned session workdir."""
    resolved = store.resolve_session_path(session, path, must_exist=True)
    if not resolved.is_file():
        raise IsADirectoryError(str(resolved))
    expected_size = resolved.stat().st_size
    _assert_image_size(expected_size, config.max_view_image_bytes)
    with resolved.open("rb") as handle:
        data = handle.read(expected_size + 1)
    if len(data) != expected_size:
        raise RuntimeError("Image changed while it was being read")
    _assert_image_size(len(data), config.max_view_image_bytes)
    image_format, mime_type = detect_image_type(data[:16])
    return _ImageFile(
        path=relative_display_from_root(resolved, config.workspace_root),
        data=data,
        format=image_format,
        mime_type=mime_type,
        size=len(data),
    )


async def read_image_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    path: str,
    session_id: str,
) -> tuple[AgentSession, _ImageFile]:
    """Load one image through explicit executor-owned session authority."""
    session = store.touch_session(session_id)
    image = await asyncio.to_thread(_local_image, config, store, path, session)
    return session, image


async def view_image_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    path: str,
    session_id: str,
) -> CallToolResult:
    """Return native MCP image content plus structured session metadata."""
    session, image = await read_image_execute(config, store, path, session_id)
    metadata = ViewImageOutput(
        session_id=session.session_id,
        path=image.path,
        mime_type=image.mime_type,
        bytes=image.size,
    )
    return CallToolResult(
        content=[
            ImageContent(
                type="image",
                data=base64.b64encode(image.data).decode("ascii"),
                mimeType=image.mime_type,
            ),
            TextContent(
                type="text",
                text=f"{image.path} ({image.mime_type}, {image.size} bytes)",
            ),
        ],
        structuredContent=metadata.model_dump(mode="json"),
    )
