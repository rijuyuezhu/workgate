"""Native Windows Task Scheduler smoke with a disposable, owned test task."""

import contextlib
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Native Windows Python required")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.platform.windows_task import WindowsUserTask  # noqa: E402


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise RuntimeError(
            f"PowerShell failed: {result.returncode}: {result.stderr}"
        )
    return result


def remove_test_directory(root: Path) -> None:
    # The stopped Task Scheduler process may briefly retain its cwd handle.
    for _ in range(60):
        try:
            shutil.rmtree(root)
            return
        except PermissionError:
            time.sleep(0.1)
    shutil.rmtree(root)


def process_is_dead(pid: int) -> bool:
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
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


def main() -> None:
    name = f"Workgate-Test-388-{os.getpid()}"
    task = WindowsUserTask(
        name, description="Managed by Workgate: disposable test", run=run
    )
    with contextlib.ExitStack() as stack:
        root = (
            Path(tempfile.gettempdir())
            / f"workgate O'Brien 中文 {uuid.uuid4().hex}"
        )
        # Like the real service directory, inherit the user's ACL rather than
        # tempfile.mkdtemp()'s special owner-only directory ACL.
        root.mkdir()
        stack.callback(remove_test_directory, root)
        marker = root / "started.txt"
        script = root / "runner.py"
        script.write_text(
            "import sys, time, os\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        assert pythonw.is_file(), pythonw
        command = [str(pythonw), str(script), str(marker)]
        assert task.status() is None
        try:
            task.install(command, cwd=str(root))
            assert task.status() is not None
            scheduled = run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"$task = Get-ScheduledTask -TaskPath '\\' -TaskName '{name}'; "
                    "[Console]::Out.WriteLine($task.Triggers[0].CimClass.CimClassName); "
                    "[Console]::Out.WriteLine($task.Settings.RestartCount)",
                ]
            ).stdout.splitlines()
            assert scheduled == ["MSFT_TaskLogonTrigger", "3"], scheduled
            task.start()
            for _ in range(100):
                if marker.is_file():
                    break
                time.sleep(0.1)
            assert marker.is_file(), (
                task.status(),
                command,
                list(root.iterdir()),
            )
            child_pid = int(marker.read_text(encoding="utf-8"))
            assert not process_is_dead(child_pid)
            assert task.status()["state"].lower() == "running"
            task.stop()
            for _ in range(100):
                if task.status()["state"].lower() != "running":
                    break
                time.sleep(0.1)
            assert task.status()["state"].lower() != "running"
            for _ in range(100):
                if process_is_dead(child_pid):
                    break
                time.sleep(0.1)
            assert process_is_dead(child_pid), "stopped task left Python alive"
        finally:
            task.uninstall()
        assert task.status() is None

        # Colocate owns two role children via its existing Windows Job Object.
        # Task Scheduler must stop the launcher and its descendants together.
        source_dir = Path(__file__).resolve().parents[2] / "src"
        script.write_text(
            "import os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(source_dir)!r})\n"
            "from workgate.platform.windows_job import WindowsJob\n"
            "job = WindowsJob()\n"
            "job.attach_current_process()\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(45)'], creationflags=0x08000000)\n"
            "Path(sys.argv[1]).write_text(f'{os.getpid()},{child.pid}')\n"
            "time.sleep(45)\n",
            encoding="utf-8",
        )
        marker.unlink(missing_ok=True)
        task.install(command, cwd=str(root))
        try:
            task.start()
            for _ in range(100):
                if marker.is_file():
                    break
                time.sleep(0.1)
            assert marker.is_file(), task.status()
            parent_pid, child_pid = (
                int(value) for value in marker.read_text().split(",")
            )
            assert not process_is_dead(parent_pid)
            assert not process_is_dead(child_pid)
            task.stop()
            for _ in range(100):
                if process_is_dead(parent_pid) and process_is_dead(child_pid):
                    break
                time.sleep(0.1)
            assert process_is_dead(parent_pid), "colocate owner survived stop"
            assert process_is_dead(child_pid), "colocate child survived stop"
        finally:
            task.uninstall()
        assert task.status() is None
    print(
        "Native Windows pythonw service and colocate Job Object child cleanup: PASS"
    )


if __name__ == "__main__":
    main()
