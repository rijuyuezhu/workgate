"""Native Windows Job Objects kill *descendants*, not only their parent."""

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Run with native Windows CPython")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.platform.windows_job import WindowsJob  # noqa: E402


def wait_dead(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True
    try:
        return kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        kernel.CloseHandle(handle)


def child_main() -> None:
    # The gate prevents the child from spawning anything until assigned to
    # the Job Object, avoiding a start-then-attach escape window.
    if sys.stdin.buffer.read(1) != b"G":
        raise RuntimeError("missing job startup gate")
    descendant = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(40)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(descendant.pid, flush=True)
    import time

    time.sleep(40)


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-native-job-"):
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        try:
            with WindowsJob() as job:
                job.attach_waiting_process(proc.pid)
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(b"G")
                proc.stdin.flush()
                descendant_pid = int(proc.stdout.readline().strip())
                assert descendant_pid != proc.pid
            # Closing the job kills both the waiting parent and its child.
            assert wait_dead(proc.pid), f"parent leaked: {proc.pid}"
            assert wait_dead(descendant_pid), (
                f"descendant leaked: {descendant_pid}"
            )
        finally:
            proc.kill() if proc.poll() is None else None
            proc.wait(timeout=10)
    print("Windows Job Object kills descendant on handle close: PASS")


if __name__ == "__main__":
    if "--child" in sys.argv:
        child_main()
    else:
        run()
