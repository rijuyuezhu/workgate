"""ChatGPT connector-compatible read-only workspace search/fetch tools."""

from ...schemas.input_models.session import SessionIdArg
from ..declarative import DeclarativeToolRegistry
from ..ops.workspace_connector import (
    fetch_error_output,
    fetch_execute,
    search_error_output,
    search_execute,
)
from ..schemas.input_models.workspace_connector import (
    ConnectorFetchIdArg,
    ConnectorSearchQueryArg,
)
from ..schemas.result_models.workspace_connector import (
    FetchOutput,
    SearchOutput,
)


class WorkspaceConnectorToolRegistry(DeclarativeToolRegistry):
    """Register the special read-only search/fetch surface for connector clients.

    These tools are intentionally separate from the richer coding-agent file and
    search tools. Regular ChatGPT custom connectors and Deep Research-style
    clients often expose only a document-source pattern: search for result cards,
    then fetch one result by id. They may not surface general-purpose tools such
    as code search, file reads, shell commands, edits, or remote-worker operations unless
    the client is in Developer Mode or otherwise supports the full MCP tool set.

    search/fetch use mcp_security_profile="connector_compatible" so MCP
    clients can discover them as document-source tools. oauth_scopes remains
    the server-enforced source of truth; the connector profile only affects MCP
    securitySchemes metadata.
    """

    name = "workspace_connector"
    """Registry group name used for tool-surface organization."""


workspace_connector_tool = WorkspaceConnectorToolRegistry.get_tool_decorator()


@workspace_connector_tool(
    http_method="POST",
    http_path="/tools/workspace_search",
    mcp_security_profile="connector_compatible",
    oauth_scopes=("shell:read",),
    annotations="read_only",
    mcp_error_handler=search_error_output,
)
async def workspace_search(
    session_id: SessionIdArg, query: ConnectorSearchQueryArg
) -> SearchOutput:
    """Search text files on the executor bound to session_id and return connector-compatible result cards. This broad read-only search uses that executor's configured workspace root; call fetch with the same session_id for a returned result id."""
    return await search_execute(query)


@workspace_connector_tool(
    http_method="POST",
    http_path="/tools/fetch",
    mcp_security_profile="connector_compatible",
    oauth_scopes=("shell:read",),
    annotations="read_only",
    mcp_error_handler=fetch_error_output,
)
async def fetch(
    session_id: SessionIdArg, id: ConnectorFetchIdArg
) -> FetchOutput:
    """Fetch one UTF-8 workspace text file from the executor bound to session_id. The id should normally come from workspace_search on the same session. For coding-agent work, prefer read because it returns grounding metadata for safe edits."""
    return await fetch_execute(id)
