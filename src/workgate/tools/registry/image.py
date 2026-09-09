"""Native MCP image-viewing tool registry."""

from ...schemas.input_models.image import ImagePathArg
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.image import ViewImageOutput
from ..declarative import DeclarativeToolRegistry


class ImageToolRegistry(DeclarativeToolRegistry):
    """Register session-bound native image viewing."""

    name = "image"
    """Registry group name used for tool-surface organization."""


image_tool = ImageToolRegistry.get_tool_decorator()


@image_tool(
    http_method=None,
    http_path=None,
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def view_image(
    session_id: SessionIdArg,
    path: ImagePathArg,
) -> ViewImageOutput:
    """View a PNG, JPEG, GIF, or WebP file as native MCP image content. The path resolves inside the shared session workdir on its bound executor; use this instead of read when visual inspection is needed."""
    del session_id, path
    raise RuntimeError("view_image requires control routing")
