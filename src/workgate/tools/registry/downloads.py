"""Tokenized public file download link tool registry."""

from ...config.settings import Settings
from ...schemas.input_models.downloads import (
    DownloadFilenameArg,
    DownloadPathArg,
    DownloadTokenArg,
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


def _download_tools_enabled(settings: Settings) -> bool:
    return settings.file_download_enabled and settings.mode in {"http", "mcp"}


def _create_file_link_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Create a temporary tokenized HTTP URL for an immutable creation-time snapshot of one existing regular file in an executor-backed agent session. Snapshot creation reads the file through the executor bound to session_id; after the snapshot succeeds, the public URL remains independent of executor availability. By default the browser downloads it as an attachment; set inline=true only when browser rendering is desired. The response includes a sensitive token and URL. Current TTL default/cap: {settings.file_download_default_ttl_s}/{settings.file_download_max_ttl_s} seconds. Current file-size cap: {settings.file_download_max_file_bytes} bytes, with 0 meaning no configured cap."""


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
    """List tokenized file download links created by this session."""
    del session_id, include_expired
    raise RuntimeError("list_file_links requires control routing")


@download_tool(
    http_method="POST",
    http_path="/tools/file_link/revoke",
    oauth_scopes=("shell:read", "file:share"),
    enabled=_download_tools_enabled,
)
async def revoke_file_link(
    session_id: SessionIdArg, token: DownloadTokenArg
) -> RevokeFileLinkOutput:
    """Revoke a tokenized file download link created by this session."""
    del session_id, token
    raise RuntimeError("revoke_file_link requires control routing")
