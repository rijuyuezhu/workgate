"""Native Windows colocate-style parent Job lifetime and descendant teardown."""

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Native Windows Python required")

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
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return True
    try:
        return kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        kernel.CloseHandle(handle)


def await_pidfile(file: Path) -> int:
    for _ in range(100):
        if file.exists():
            return int(file.read_text(encoding="ascii"))
        time.sleep(0.1)
    raise AssertionError(f"PID file not published: {file}")


def role_main(root: Path) -> None:
    grandchild = subprocess.Popen(
        [sys.executable, str(Path(__file__)), "--grandchild"],
        creationflags=0x08000000,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (root / "grand.pid").write_text(str(grandchild.pid), encoding="ascii")
    time.sleep(60)


def colocate_main(root: Path, hold: bool) -> None:
    job = WindowsJob()
    job.attach_current_process()
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__)), "--role", str(root)],
        creationflags=0x08000000,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (root / "role.pid").write_text(str(child.pid), encoding="ascii")
    await_pidfile(root / "grand.pid")
    if hold:
        time.sleep(60)
    # Do not close the job while parent is still inside it. The OS closes its
    # noninheritable handle when this launcher exits (normal or forced).
    os._exit(0)


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-colocate-job-") as temp:
        base = Path(temp)
        for mode in ("normal", "forced"):
            root = base / mode
            root.mkdir()
            parent = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__)),
                    "--parent",
                    str(root),
                    mode,
                ],
                creationflags=0x08000000,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                child = await_pidfile(root / "role.pid")
                grandchild = await_pidfile(root / "grand.pid")
                if mode == "forced":
                    parent.kill()
                parent.wait(timeout=12)
                assert wait_dead(child), f"{mode} parent leaked role {child}"
                assert wait_dead(grandchild), (
                    f"{mode} parent leaked grandchild {grandchild}"
                )
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
    print(
        "Windows colocate parent Job cleans descendants on normal/forced exit: PASS"
    )


if __name__ == "__main__":
    if "--grandchild" in sys.argv:
        time.sleep(60)
    elif "--role" in sys.argv:
        role_main(Path(sys.argv[2]))
    elif "--parent" in sys.argv:
        colocate_main(Path(sys.argv[2]), sys.argv[3] == "forced")
    else:
        run()
