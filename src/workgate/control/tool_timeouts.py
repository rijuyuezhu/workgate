"""Control-owned public tool watchdog policy."""

from ..config.control import ControlConfig, get_control_config

# apply_patch keeps a control-owned minimum budget for its multi-phase RPC.
APPLY_PATCH_WATCHDOG_TIMEOUT_S = 45


def tool_timeout_s(
    tool_name: str | None = None, *, config: ControlConfig | None = None
) -> float:
    """Return the effective control-side MCP/HTTP watchdog for one tool."""
    active = config or get_control_config()
    configured = max(0.001, active.tool_timeout_s)
    if tool_name == "apply_patch":
        return max(configured, float(APPLY_PATCH_WATCHDOG_TIMEOUT_S))
    return configured
