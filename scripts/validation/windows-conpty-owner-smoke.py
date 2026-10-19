"""Native ConPTY child lifetime when owning Executor terminates abruptly."""

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Run on native Windows CPython")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def is_dead(pid: int) -> bool:
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
        return kernel.WaitForSingleObject(handle, 1500) == 0
    finally:
        kernel.CloseHandle(handle)


def child_main(pidfile: Path) -> None:
    from workgate.platform.windows_conpty import spawn_conpty

    session = spawn_conpty(
        ["powershell.exe", "-NoProfile", "-NoLogo"],
        cwd=str(pidfile.parent),
    )
    pidfile.write_text(str(session.process.pid), encoding="ascii")
    time.sleep(50)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wg-conpty-owner-") as temp:
        pidfile = Path(temp) / "shell.pid"
        executor = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                str(pidfile),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        shell_pid = None
        try:
            for _ in range(80):
                if pidfile.is_file():
                    shell_pid = int(pidfile.read_text(encoding="ascii"))
                    break
                if executor.poll() is not None:
                    raise AssertionError(
                        f"PTY owner exited early: {executor.returncode}"
                    )
                time.sleep(0.1)
            assert shell_pid, "ConPTY child PID not published"
            executor.kill()
            executor.wait(timeout=10)
            dead = is_dead(shell_pid)
            if not dead:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(shell_pid)],
                    capture_output=True,
                    check=False,
                )
            assert dead, (
                f"ConPTY shell survives abrupt Executor kill: {shell_pid}"
            )
        finally:
            if executor.poll() is None:
                executor.kill()
                executor.wait(timeout=5)
    print("ConPTY owner abrupt stop kills shell: PASS")


if __name__ == "__main__":
    if "--child" in sys.argv:
        child_main(Path(sys.argv[2]))
    else:
        main()
