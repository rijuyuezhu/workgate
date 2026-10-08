"""Control-owned public tool watchdog policy."""

from collections.abc import Mapping
from typing import Any

from ..config.control import ControlConfig, get_control_config

# apply_patch keeps a control-owned minimum budget for its multi-phase RPC.
APPLY_PATCH_WATCHDOG_TIMEOUT_S = 45
# Allow bounded shell commands to finish their own timeout and descendant cleanup.
SHELL_COMMAND_TOOL_NAMES = frozenset(
    {"bash", "run_python_code", "browser_run_script"}
)
_SHELL_CLEANUP_MARGIN_S = 15
_MAX_WATCHDOG_COMMAND_S = 3600


def tool_timeout_s(
    tool_name: str | None = None,
    *,
    config: ControlConfig | None = None,
    args: Mapping[str, Any] | None = None,
) -> float:
    """Return the effective control-side MCP/HTTP watchdog for one tool."""
    active = config or get_control_config()
    configured = max(0.001, active.tool_timeout_s)
    if tool_name == "apply_patch":
        return max(configured, float(APPLY_PATCH_WATCHDOG_TIMEOUT_S))
    if (
        tool_name in SHELL_COMMAND_TOOL_NAMES
        and args is not None
        and not args.get("async_", False)
        and not args.get("pty", False)
    ):
        requested = args.get("timeout_s")
        if requested is None and tool_name == "browser_run_script":
            requested = 60
        if type(requested) is int and requested > 0:
            return max(
                configured,
                float(min(requested, _MAX_WATCHDOG_COMMAND_S))
                + _SHELL_CLEANUP_MARGIN_S,
            )
    return configured
