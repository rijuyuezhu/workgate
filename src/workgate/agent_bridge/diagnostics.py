"""Bounded, credential-free static status for Control/Executor bridge settings.

This is an on-demand diagnostic, not MCP discovery: it never contacts upstream
servers or reads secret material into the API response.
"""

from pathlib import Path
from typing import Any, Literal

from .state import load_agent_manifest


def manifest_diagnostics(
    config_dir: Path, *, owner: Literal["control", "executor"]
) -> dict[str, Any]:
    """Report manifest validity and locally owned MCP entries only."""
    try:
        path = config_dir / "config.json"
        # Reject symlinks before inspecting file metadata; a read-only status
        # endpoint should not traverse an arbitrary target even for its size.
        if config_dir.is_symlink() or path.is_symlink():
            return {"status": "invalid_config", "errors": 1, "servers": []}
        if path.exists() and path.stat().st_size > 1_048_576:
            return {"status": "unavailable", "errors": 1, "servers": []}
        loaded = load_agent_manifest(config_dir, read_only=True)
    except OSError, ValueError:
        return {"status": "unavailable", "errors": 1, "servers": []}
    transport = "http" if owner == "control" else "stdio"
    servers = [
        {"name": name[:80], "enabled": server.enabled, "type": server.type}
        for name, server in sorted(loaded.data.mcp_servers.items())
        if server.type == transport
    ][:32]
    return {
        "status": loaded.status,
        "errors": min(len(loaded.errors), 100),
        "servers": servers,
        "truncated": sum(
            1
            for server in loaded.data.mcp_servers.values()
            if server.type == transport
        )
        > 32,
    }
