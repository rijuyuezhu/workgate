"""Host-selected process creation capabilities.

Subsequent Windows support must provide descendant containment before enabling
bounded command launches. Never silently fall back to uncontained processes.
"""

import asyncio
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


async def terminate_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    graceful_timeout_s: int = 5,
    kill_timeout_s: int = 2,
    system: str | None = None,
) -> str:
    """Terminate the isolated child group, including timeout escalation.

    Only the outer process group is controlled here; internal bounded-runner
    descendant containment remains owned by that runner (see #385).
    """
    current = host_os(system)
    if current in (HostOS.LINUX, HostOS.MACOS):
        from .posix.process import terminate_process_group

        return await terminate_process_group(
            proc,
            graceful_timeout_s=graceful_timeout_s,
            kill_timeout_s=kill_timeout_s,
        )
    raise RuntimeError(
        f"process-tree termination is unsupported on {system or current or 'this host'}"
    )
