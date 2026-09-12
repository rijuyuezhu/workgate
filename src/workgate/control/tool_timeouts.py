"""Control-owned public tool watchdog policy."""

from ..config.settings import get_settings

# apply_patch keeps a control-owned minimum budget for its multi-phase RPC.
APPLY_PATCH_WATCHDOG_TIMEOUT_S = 45


def tool_timeout_s(tool_name: str | None = None) -> float:
    """Return the effective control-side MCP/HTTP watchdog for one tool."""
    settings = get_settings()
    configured = max(0.001, settings.tool_timeout_s)
    if tool_name == "apply_patch":
        return max(configured, float(APPLY_PATCH_WATCHDOG_TIMEOUT_S))
    return configured
