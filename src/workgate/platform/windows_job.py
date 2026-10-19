"""Native Windows Job Object for bounded descendant lifetime management.

Windows has no POSIX process groups. Keep the platform-specific API surface
small: create a kill-on-close job and attach only a process that has not yet
been released to launch untrusted commands.
"""

import asyncio
import contextlib
import ctypes
import os
import threading
from ctypes import wintypes
from typing import Any

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_int64),
        ("per_job_user_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io_counters", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


def _win_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())  # pyright: ignore[reportAttributeAccessIssue]


class WindowsJob:
    """Own one kill-on-close job; no unsafe late-attach convenience methods."""

    _kernel: Any
    _close_lock: threading.Lock

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects require native Windows")
        kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue]
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        self._close_lock = threading.Lock()
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise _win_error()
        self._handle = handle
        limits = _ExtendedLimits()
        limits.basic.flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = _win_error()
            self.close()
            raise error

    def attach_waiting_process(self, pid: int) -> None:
        """Caller must guarantee the child cannot launch work before return."""
        process = self._kernel.OpenProcess(
            _PROCESS_SET_QUOTA
            | _PROCESS_TERMINATE
            | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not process:
            raise _win_error()
        try:
            if not self._kernel.AssignProcessToJobObject(self._handle, process):
                raise _win_error()
        finally:
            self._kernel.CloseHandle(process)

    def attach_current_process(self) -> None:
        """Attach a launcher before it creates any descendants."""
        self.attach_waiting_process(os.getpid())

    def close(self) -> None:
        """Terminate all attached processes and descendants exactly once."""
        with self._close_lock:
            handle = self._handle
            self._handle = None
            if handle and not self._kernel.CloseHandle(handle):
                raise _win_error()

    def __enter__(self) -> WindowsJob:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _kill_unreleased_process(
    launch: asyncio.Task[asyncio.subprocess.Process],
) -> None:
    """Reap a process created after its caller was cancelled during startup."""
    if launch.cancelled():
        return
    try:
        proc = launch.result()
    except Exception:
        return
    if proc.stdin is not None:
        proc.stdin.close()
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    asyncio.create_task(proc.wait())


async def spawn_bounded_runner(
    argv: list[str], *, cwd: str, env: dict[str, str]
) -> asyncio.subprocess.Process:
    """Gate execution until the native job owns the process and descendants."""
    job = WindowsJob()  # Must succeed before the child is created.
    proc: asyncio.subprocess.Process | None = None
    launch: asyncio.Task[asyncio.subprocess.Process] | None = None
    try:
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        )
        # Shield startup so a late child is always killed and reaped.
        proc = await asyncio.shield(launch)
        assert proc is not None
        job.attach_waiting_process(proc.pid)
        proc._workgate_windows_job = job  # pyright: ignore[reportAttributeAccessIssue]
        assert proc.stdin is not None
        proc.stdin.write(b"\x01")
        await proc.stdin.drain()
        proc.stdin.close()
        return proc
    except BaseException:
        job.close()
        if proc is not None:
            if proc.stdin is not None:
                proc.stdin.close()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            asyncio.create_task(proc.wait())
        elif launch is not None:
            if launch.done():
                _kill_unreleased_process(launch)
            else:
                launch.add_done_callback(_kill_unreleased_process)
        raise


def close_process_job(proc: asyncio.subprocess.Process) -> None:
    """Close after normal completion or cancellation, killing leftover descendants."""
    job: WindowsJob | None = getattr(proc, "_workgate_windows_job", None)
    if job is None:
        raise RuntimeError("Windows process was not assigned to a Job Object")
    job.close()


async def terminate_process_job(
    proc: asyncio.subprocess.Process, *, timeout_s: int
) -> str:
    """Kill the entire job first, then reap its root process."""
    close_process_job(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        return "Windows Job Object process did not exit after termination"
    return ""
