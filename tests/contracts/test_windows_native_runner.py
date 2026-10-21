"""Actual Windows ConPTY and descendant containment; never mocked or WSL."""

import asyncio
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workgate.platform.shell import shell_join_argv
from workgate.platform.windows_conpty import is_available, spawn_conpty
from workgate.platform.windows_job import (
    close_process_job,
    spawn_bounded_runner,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="native Windows only"
)


def test_real_conpty_input_output_and_shutdown(tmp_path: Path) -> None:
    assert is_available(), "Windows native pywinpty dependency is required"
    shell = spawn_conpty(
        ["powershell.exe", "-NoLogo", "-NoProfile"], cwd=str(tmp_path)
    )
    try:
        reader = shell.subscribe()
        shell.resize(95, 34)
        shell.write("Write-Output ('WG391_' + 'NATIVE_CONPTY_OK')\r")
        deadline = time.monotonic() + 12
        output = bytearray()
        while time.monotonic() < deadline:
            chunk, _finished = reader.read_wait(4096, 200)
            output.extend(chunk)
            if b"WG391_NATIVE_CONPTY_OK" in output:
                break
        assert b"WG391_NATIVE_CONPTY_OK" in output
    finally:
        shell.close()
    assert shell.closed


def test_real_conpty_natural_exit_delivers_eof(tmp_path: Path) -> None:
    shell = spawn_conpty(
        ["powershell.exe", "-NoLogo", "-NoProfile"], cwd=str(tmp_path)
    )
    try:
        reader = shell.subscribe()
        # Subscribe before triggering natural exit so a fast process cannot
        # finish before an observer attaches. Avoid accepting echoed input.
        shell.write("Write-Output ('WG_' + 'NATIVE_EOF'); exit\r")
        output = bytearray()
        deadline = time.monotonic() + 12
        eof = False
        while time.monotonic() < deadline and not eof:
            chunk, eof = reader.read_wait(4096, 250)
            output.extend(chunk)
        assert b"WG_NATIVE_EOF" in output
        assert eof and shell.ended, "natural ConPTY exit did not reach EOF"
    finally:
        shell.close()
    assert shell.closed


def _windows_pid_exited(pid: int, wait_ms: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue]
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER means the PID no longer exists; denied access
        # is not evidence that the child exited.
        return ctypes.get_last_error() == 87  # pyright: ignore[reportAttributeAccessIssue]
    try:
        return kernel.WaitForSingleObject(handle, wait_ms) == 0
    finally:
        kernel.CloseHandle(handle)


def test_real_conpty_close_reaps_descendant(tmp_path: Path) -> None:
    """Closing a PowerShell ConPTY must also stop the process it spawned."""
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "spawn child.py"
    script.write_text(
        "import subprocess,sys,time,pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(40)'],stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')\n"
        "time.sleep(40)\n",
        encoding="utf-8",
    )
    shell = spawn_conpty(
        ["powershell.exe", "-NoLogo", "-NoProfile"], cwd=str(tmp_path)
    )
    pid = None
    try:
        shell.write(
            shell_join_argv(
                [sys.executable, str(script), str(pidfile)],
                shell="powershell.exe",
            )
            + "\r"
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.05)
        assert pidfile.is_file(), "ConPTY child process was never spawned"
        pid = int(pidfile.read_text(encoding="ascii"))
        assert not _windows_pid_exited(pid, 0), (
            "child exited before ConPTY closed"
        )
        shell.close()
        assert _windows_pid_exited(pid, 5000), "ConPTY close leaked a child"
    finally:
        shell.close()
        if pid is not None and not _windows_pid_exited(pid, 0):
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )


@pytest.mark.asyncio
async def test_real_windows_job_gates_bounded_subprocess(
    tmp_path: Path,
) -> None:
    process = await spawn_bounded_runner(
        [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(1); print('WG391_JOB_OK' if data == b'\\x01' else 'BAD_GATE')",
        ],
        cwd=str(tmp_path),
        env=os.environ.copy(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=12
        )
        assert process.returncode == 0, stderr
        assert b"WG391_JOB_OK" in stdout
    finally:
        close_process_job(process)


@pytest.mark.asyncio
async def test_real_windows_job_cancellation_reaps_process(
    tmp_path: Path,
) -> None:
    """Job Object cancellation must kill the native child, not leave it alive."""
    from workgate.platform.windows_job import terminate_process_job

    process = await spawn_bounded_runner(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(1); print('WG391_READY',flush=True); time.sleep(120)",
        ],
        cwd=str(tmp_path),
        env=os.environ.copy(),
    )
    try:
        assert process.stdout is not None
        ready = await asyncio.wait_for(process.stdout.readline(), timeout=12)
        assert b"WG391_READY" in ready
        assert await terminate_process_job(process, timeout_s=10) == ""
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            close_process_job(process)
            await asyncio.wait_for(process.wait(), timeout=10)


def test_real_windows_file_lock_contended_across_processes(
    tmp_path: Path,
) -> None:
    """Use real msvcrt handles, not a patched Windows API."""
    from workgate.utils.private_files import _try_lock_handle

    locked = tmp_path / "resource.lock"
    locked.write_bytes(b"\0")
    ready = tmp_path / "locked.ready"
    program = (
        "import msvcrt,sys; from pathlib import Path; "
        "f=open(sys.argv[1],'r+b'); msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); "
        "Path(sys.argv[2]).write_text('ready'); sys.stdin.buffer.read(1)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(locked), str(ready)],
        stdin=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 8
        while not ready.is_file() and time.monotonic() < deadline:
            if child.poll() is not None:
                pytest.fail(
                    f"Windows lock child exited early: {child.returncode}"
                )
            time.sleep(0.05)
        assert ready.is_file(), "native Windows lock child did not start"
        with locked.open("r+b") as handle:
            assert not _try_lock_handle(handle)
    finally:
        assert child.stdin is not None
        child.stdin.close()
        try:
            child.wait(timeout=6)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
