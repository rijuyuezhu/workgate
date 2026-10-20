"""Actual Windows ConPTY and descendant containment; never mocked or WSL."""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

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
