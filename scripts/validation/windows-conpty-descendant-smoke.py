"""Check whether an interactive ConPTY shell kill also removes launched children."""

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Windows only")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from workgate.platform.shell import shell_join_argv  # noqa: E402
from workgate.platform.windows_conpty import spawn_conpty  # noqa: E402


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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wg386-descendants-") as temp:
        root = Path(temp)
        pidfile = root / "child.pid"
        script = root / "spawn child.py"
        script.write_text(
            "import subprocess,sys,time,pathlib\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(40)'],stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')\n"
            "time.sleep(40)\n",
            encoding="utf-8",
        )
        session = spawn_conpty(
            ["powershell.exe", "-NoLogo", "-NoProfile"], cwd=str(root)
        )
        pid = None
        try:
            session.write(
                shell_join_argv(
                    [sys.executable, str(script), str(pidfile)],
                    shell="powershell.exe",
                )
                + "\r"
            )
            for _ in range(100):
                if pidfile.exists():
                    pid = int(pidfile.read_text(encoding="ascii"))
                    break
                time.sleep(0.1)
            assert pid is not None, "grandchild was never spawned"
            session.close()
            gone = wait_dead(pid)
            print("ConPTY close removed detached descendant:", gone, flush=True)
            assert gone, "ConPTY kill leaked a descendant"
        finally:
            session.close()
            if pid is not None and not wait_dead(pid):
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                )


if __name__ == "__main__":
    main()
