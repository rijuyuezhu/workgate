"""Native Windows tracked job runners must reap descendants and record results."""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Run on native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from workgate.platform.shell import shell_join_argv  # noqa: E402


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


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-durable-job-") as tmp:
        root = Path(tmp)
        parent = root / "spawn child.py"
        parent.write_text(
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c',"
            "'import time; time.sleep(60)'], stdin=subprocess.DEVNULL,"
            " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
            "print('TRACKED_JOB_OK', flush=True)\n"
            "if len(sys.argv) > 2: time.sleep(60)\n"
            "sys.exit(7)\n",
            encoding="utf-8",
        )

        def runner_args(
            suffix: str, *, linger: bool
        ) -> tuple[list[str], Path, Path]:
            pidfile = root / f"{suffix}.pid"
            command = root / f"{suffix}.command"
            log = root / f"{suffix}.log"
            status = root / f"{suffix}.status.json"
            command.write_text(
                shell_join_argv(
                    [
                        sys.executable,
                        str(parent),
                        str(pidfile),
                        *(["wait"] if linger else []),
                    ],
                    shell="powershell.exe",
                ),
                encoding="utf-8",
            )
            return (
                [
                    sys.executable,
                    "-m",
                    "workgate.executor.jobs.runner",
                    "--command-file",
                    str(command),
                    "--log-file",
                    str(log),
                    "--status-file",
                    str(status),
                    "--cwd",
                    str(root),
                    "--shell",
                    "powershell.exe",
                    "--max-log-bytes",
                    "20000",
                ],
                pidfile,
                status,
            )

        # The test itself uses a transient uv wheel; ensure the separately
        # launched internal runner uses this checkout's pending source edits.
        runner_env = os.environ.copy()
        runner_env["PYTHONPATH"] = str(
            Path(__file__).resolve().parents[2] / "src"
        )
        argv, pidfile, statusfile = runner_args("natural", linger=False)
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=runner_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, err = process.communicate(timeout=18)
            assert process.returncode == 7, (process.returncode, out, err)
            status = json.loads(statusfile.read_text(encoding="utf-8"))
            assert status["exit_code"] == 7 and status["error"] is None, status
            assert "TRACKED_JOB_OK" in (root / "natural.log").read_text(
                encoding="utf-8"
            )
            pid = int(pidfile.read_text(encoding="ascii"))
            assert wait_dead(pid), (
                f"normal job completion leaked descendant {pid}"
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        argv, pidfile, _statusfile = runner_args("forced", linger=True)
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=runner_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            for _ in range(100):
                if pidfile.exists():
                    break
                time.sleep(0.1)
            assert pidfile.exists(), "forced stop child did not start"
            pid = int(pidfile.read_text(encoding="ascii"))
            process.kill()
            process.communicate(timeout=12)
            assert wait_dead(pid), (
                f"killing durable runner leaked descendant {pid}"
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    print(
        "Windows durable runner logs/status, completion and forced-stop descendants: PASS"
    )


if __name__ == "__main__":
    run()
