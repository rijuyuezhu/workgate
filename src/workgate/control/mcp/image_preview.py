"""Inline MCP App preview for existing expiring Workgate image links."""

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations

from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_FILE_SHARE, SCOPE_SHELL_READ
from ...tools.metadata import mcp_security_meta
from ..download_store import ClaimedDownload, release_claim
from ..downloads import DOWNLOAD_PREFIX, claim_download
from ..runtime import ControlRuntime

_RESOURCE = Path(__file__).with_name("image_preview.html")
_RESOURCE_URI = "ui://workgate/image-preview.html"
_RESOURCE_MIME = "text/html;profile=mcp-app"
_IMAGE_TYPES = {"image/jpeg", "image/png"}


def _versioned_uri() -> str:
    digest = hashlib.sha256(_RESOURCE.read_bytes()).hexdigest()[:16]
    return f"ui://workgate/image-preview-{digest}.html"


def _validate_image_link(url: str, runtime: ControlRuntime) -> str:
    """Accept only an existing inline image snapshot issued by this Control."""
    prefix = (
        runtime.config.resolved_base_url.rstrip("/") + DOWNLOAD_PREFIX + "/"
    )
    if not url.startswith(prefix):
        raise ToolError("Expected a Workgate image download URL")
    token = url[len(prefix) :]
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise ToolError("Invalid Workgate image download URL")
    claimed = claim_download(token, consume=False, settings=runtime.config)
    if not isinstance(claimed, ClaimedDownload):
        raise ToolError("Image link is unavailable or expired")
    try:
        if (
            not claimed.link["inline"]
            or claimed.link["media_type"] not in _IMAGE_TYPES
        ):
            raise ToolError("Link must be an inline JPEG or PNG image")
        return str(claimed.link["media_type"])
    finally:
        release_claim(claimed)


def register_image_preview(
    mcp: MCPServer, runtime: ControlRuntime | None
) -> None:
    """Register the HTTP-only image preview resource and model-facing tool."""
    if (
        runtime is None
        or runtime.config.mode == "stdio"
        or not runtime.config.ui_enabled
        or not runtime.config.file_download_enabled
    ):
        return

    resource_uri = _versioned_uri()
    parsed = urlparse(runtime.config.resolved_base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    resource_options: dict[str, Any] = {
        "name": "workgate-image-preview",
        "title": "Workgate Image Preview",
        "description": "Show an expiring Workgate image snapshot in the conversation.",
        "mime_type": _RESOURCE_MIME,
        "meta": {
            "ui": {
                "domain": origin,
                "csp": {"connectDomains": [], "resourceDomains": [origin]},
                "prefersBorder": True,
            },
            "openai/widgetDomain": origin,
            "openai/widgetDescription": "Preview an expiring Workgate image snapshot.",
            "openai/widgetPrefersBorder": True,
        },
    }

    @mcp.resource(_RESOURCE_URI, **resource_options)
    def image_preview_resource() -> str:
        return _RESOURCE.read_text(encoding="utf-8")

    @mcp.resource(
        "ui://workgate/image-preview-{digest}.html", **resource_options
    )
    def image_preview_versioned(digest: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{16}", digest):
            raise ResourceError("Invalid image preview resource cache key")
        return _RESOURCE.read_text(encoding="utf-8")

    scopes = (SCOPE_SHELL_READ, SCOPE_FILE_SHARE)

    @mcp.tool(
        description=(
            "Show an existing create_file_link(inline=true) JPEG or PNG URL "
            "from this Workgate Control directly in the ChatGPT conversation. "
            "The image link remains expiring and independently revocable."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        meta={
            **mcp_security_meta(scopes, settings=runtime.config),
            "ui": {"resourceUri": resource_uri},
            "ui/resourceUri": resource_uri,
            "openai/outputTemplate": resource_uri,
            "openai/widgetAccessible": True,
            "openai/toolInvocation/invoking": "Opening image preview",
            "openai/toolInvocation/invoked": "Image preview ready",
        },
        structured_output=True,
    )
    async def image_show(url: str) -> dict[str, str]:
        try:
            require_oauth_scopes(scopes)
        except MissingOAuthScopeError as exc:
            raise ToolError(str(exc)) from None
        media_type = _validate_image_link(url, runtime)
        return {"url": url, "media_type": media_type}
