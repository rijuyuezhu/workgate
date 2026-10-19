"""Host-selected process-tree lifecycle operations.

POSIX uses process groups; Windows bounded runners are attached to kill-on-close
Job Objects before receiving permission to execute.
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
    """Terminate the host-native isolated process tree.

    POSIX uses process groups and an internal descendant-reaping runner;
    Windows owns the entire descendant tree through a Job Object.
    """
    current = host_os(system)
    if current is HostOS.WINDOWS:
        from .windows_job import terminate_process_job

        return await terminate_process_job(proc, timeout_s=kill_timeout_s)
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
