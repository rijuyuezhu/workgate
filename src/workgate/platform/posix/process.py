"""POSIX process-group termination for bounded Executor subprocesses."""

import asyncio
import os
import signal


async def _wait_for_process_exit(
    proc: asyncio.subprocess.Process, timeout_s: int
) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        return True
    except TimeoutError:
        return False


async def terminate_process_group(
    proc: asyncio.subprocess.Process,
    *,
    graceful_timeout_s: int,
    kill_timeout_s: int,
) -> str:
    """Terminate an isolated process group, escalating when necessary."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        proc.terminate()

    if await _wait_for_process_exit(proc, graceful_timeout_s):
        return ""

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()

    if await _wait_for_process_exit(proc, kill_timeout_s):
        return ""
    return "Process did not exit after SIGKILL"
