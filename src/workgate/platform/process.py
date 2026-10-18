"""Host-selected process creation capabilities.

Subsequent Windows support must provide descendant containment before enabling
bounded command launches. Never silently fall back to uncontained processes.
"""

from typing import Any

from .host import HostOS, host_os


def new_process_group_kwargs(*, system: str | None = None) -> dict[str, Any]:
    """Start a POSIX child process in an independently owned session."""
    current = host_os(system)
    if current in (HostOS.LINUX, HostOS.MACOS):
        return {"start_new_session": True}
    raise RuntimeError(
        f"bounded process containment is unsupported on {system or current or 'this host'}"
    )
