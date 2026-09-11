"""Secret scanning tool registry."""

from ...schemas.input_models.secret_scan import (
    SecretScanCwdArg,
    SecretScanGlobArg,
    SecretScanMaxResultsArg,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.secret_scan import SecretScanOutput
from ..contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry


class SecretScanToolRegistry(DeclarativeToolRegistry):
    """Register secret scanning tools."""

    name = "secret_scan"
    """Registry group name used for tool-surface organization."""


secret_scan_tool = SecretScanToolRegistry.get_tool_decorator()


def _secret_scan_description(context: McpToolContext) -> str:
    del context
    return """Scan text files under an explicit agent/workspace session for common secret-like strings before commit, push, release, or sharing logs. Results are heuristic and do not prove the workspace is secret-free. The bound executor applies its configured result limit."""


@secret_scan_tool(
    http_method="POST",
    http_path="/tools/secret_scan",
    description=_secret_scan_description,
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def secret_scan(
    session_id: SessionIdArg,
    cwd: SecretScanCwdArg = ".",
    glob: SecretScanGlobArg = None,
    max_results: SecretScanMaxResultsArg = 200,
) -> SecretScanOutput:
    """Scan session workdir text files for common secret-like strings."""
    del session_id, cwd, glob, max_results
    raise RuntimeError("secret_scan requires control routing")
