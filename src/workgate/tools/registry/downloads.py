"""Tokenized public file download link tool registry."""

from ...config.control import ControlConfig
from ...schemas.input_models.downloads import (
    DownloadFilenameArg,
    DownloadLinkIdArg,
    DownloadPathArg,
    DownloadTtlArg,
    IncludeExpiredArg,
    InlineDownloadArg,
    MaxDownloadsArg,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.downloads import (
    CreateFileLinkOutput,
    ListFileLinksOutput,
    RevokeFileLinkOutput,
)
from ..contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry


class DownloadToolRegistry(DeclarativeToolRegistry):
    """Register protected tools for creating and managing download links."""

    name = "downloads"
    """Registry group name used for tool-surface organization."""


download_tool = DownloadToolRegistry.get_tool_decorator()


def _download_tools_enabled(settings: ControlConfig) -> bool:
    return settings.file_download_enabled and settings.mode in {"http", "mcp"}


def _create_file_link_description(context: McpToolContext) -> str:
    del context
    return """Create a temporary bearer URL for an immutable file snapshot. The URL grants access to its holder and remains available after the executor disconnects; retain link_id for revocation."""


@download_tool(
    http_method="POST",
    http_path="/tools/file_link/create",
    description=_create_file_link_description,
    oauth_scopes=("shell:read", "file:share"),
    enabled=_download_tools_enabled,
)
async def create_file_link(
    session_id: SessionIdArg,
    path: DownloadPathArg,
    ttl_s: DownloadTtlArg = None,
    filename: DownloadFilenameArg = None,
    max_downloads: MaxDownloadsArg = None,
    inline: InlineDownloadArg = False,
) -> CreateFileLinkOutput:
    """Create a tokenized snapshot URL for one file in an executor-backed session."""
    del session_id, path, ttl_s, filename, max_downloads, inline
    raise RuntimeError("create_file_link requires control routing")


@download_tool(
    http_method="GET",
    http_path="/tools/file_link/list",
    annotations="read_only",
    oauth_scopes=("shell:read", "file:share"),
    enabled=_download_tools_enabled,
)
async def list_file_links(
    session_id: SessionIdArg,
    include_expired: IncludeExpiredArg = False,
) -> ListFileLinksOutput:
    """List non-secret management metadata for links created by this session."""
    del session_id, include_expired
    raise RuntimeError("list_file_links requires control routing")


@download_tool(
    http_method="POST",
    http_path="/tools/file_link/revoke",
    oauth_scopes=("shell:read", "file:share"),
    enabled=_download_tools_enabled,
)
async def revoke_file_link(
    session_id: SessionIdArg, link_id: DownloadLinkIdArg
) -> RevokeFileLinkOutput:
    """Revoke a public file link by its non-secret management id."""
    del session_id, link_id
    raise RuntimeError("revoke_file_link requires control routing")
