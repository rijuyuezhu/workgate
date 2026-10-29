"""Real native Windows Task Scheduler Executor -> Control reconnect lifecycle."""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

if os.name != "nt":
    raise RuntimeError("Run this smoke on native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tests.e2e.helpers import (  # noqa: E402
    free_tcp_port,
    isolated_xdg_env,
    provision_executor_pair,
    start_control_process,
    stop_process,
    wait_for_executor_online,
    wait_for_http_ready,
    write_yaml_config,
)
from workgate.executor.connection_status import (  # noqa: E402
    ExecutorConnectionStatusStore,
)
from workgate.persistence import FileStateStore  # noqa: E402
from workgate.platform.windows_task import WindowsUserTask  # noqa: E402


def _powershell(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=30, check=True
    )


def _task_last_run(task_name: str) -> str:
    script = (
        f"(Get-ScheduledTaskInfo -TaskPath '\\' -TaskName '{task_name}')"
        ".LastRunTime.ToUniversalTime().Ticks"
    )
    return _powershell(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    ).stdout.strip()


async def main(*, outage_only: bool = False) -> None:
    root = Path(tempfile.gettempdir()) / f"wg392-paired-service-{uuid4().hex}"
    root.mkdir()
    task = WindowsUserTask(
        f"Workgate-Test-392-{uuid4().hex[:12]}",
        description="Workgate disposable native paired Executor test",
        run=_powershell,
    )
    control = None
    try:
        state = root / "control-state"
        executor_state = root / "executor-state"
        workdir = root / "workspace with spaces"
        workdir.mkdir()
        port = free_tcp_port()
        url = f"http://127.0.0.1:{port}"
        executor_id = provision_executor_pair(
            control_state_dir=state,
            executor_state_dir=executor_state,
            control_url=url,
        )
        # An elevated test launcher (e.g. WSL interop) creates Admin-owned
        # private state. The real Scheduler task uses a *limited* user token.
        # Grant only this same account access to disposable test paths.
        account = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        subprocess.run(
            ["icacls.exe", str(root), "/grant", f"{account}:(OI)(CI)F", "/T"],
            check=True,
            capture_output=True,
            timeout=20,
        )
        control_config = write_yaml_config(
            root / "control.yaml",
            {
                "mode": "http",
                "host": "127.0.0.1",
                "port": port,
                "base_url": url,
                "auth_mode": "none",
                "state_dir": str(state),
                "data_dir": str(root / "control-data"),
                "agent_bridge_enabled": False,
            },
        )
        executor_config = write_yaml_config(
            root / "executor.yaml",
            {
                "state_dir": str(executor_state),
                "default_workdir": str(workdir),
                "shell_executable": "powershell.exe",
            },
        )
        # Task Scheduler has no inherited test PYTHONPATH. Keep the launcher
        # explicit and disposable rather than touching Windows-wide settings.
        launcher = root / "run-executor.py"
        launcher.write_text(
            "import contextlib, os, sys, sysconfig, traceback\n"
            "if os.name == 'nt' and sysconfig.get_platform() == 'win-arm64':\n"
            "    os.environ['CRYPTOGRAPHY_OPENSSL_NO_LEGACY'] = '1'\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2] / 'src')!r})\n"
            f"with open({str(root / 'executor-error.log')!r}, 'a', encoding='utf-8', buffering=1) as log:\n"
            "    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):\n"
            "        try:\n"
            "            from workgate.main import main\n"
            f"            main({['executor', 'run', '--managed-service', '--config', str(executor_config)]!r})\n"
            "        except BaseException:\n"
            "            traceback.print_exc()\n"
            "            raise\n",
            encoding="utf-8",
        )
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        assert pythonw.is_file()
        assert task.status() is None
        control = start_control_process(
            control_config,
            env=isolated_xdg_env(root / "xdg"),
            stdout_path=root / "control.out",
            stderr_path=root / "control.err",
        )
        await wait_for_http_ready(
            url,
            control,
            stdout_path=root / "control.out",
            stderr_path=root / "control.err",
            timeout_s=35,
        )
        task.install([str(pythonw), str(launcher)], cwd=str(root))
        task.start()
        try:
            first_seen = await wait_for_executor_online(
                url,
                control,
                executor_id,
                timeout_s=50,
                stdout_path=root / "control.out",
                stderr_path=root / "control.err",
            )
            original_task = task.status()
            assert original_task["state"].lower() == "running"
            original_start = _task_last_run(task.name)
            profile_path = FileStateStore(
                lambda: executor_state
            ).layout.executor_profile_path
            original_profile = profile_path.read_bytes()
            connection = ExecutorConnectionStatusStore(
                FileStateStore(lambda: executor_state)
            )

            # Simulate temporary Control loss, not a managed Executor restart.
            stop_process(control)
            control = None
            deadline = asyncio.get_running_loop().time() + 25
            while not connection.describe().startswith("reconnecting"):
                assert task.status()["state"].lower() == "running"
                current = connection.describe()
                assert not current.startswith("action-required"), (
                    f"Executor incorrectly requires owner action after Control loss: {current}"
                )
                assert asyncio.get_running_loop().time() < deadline, (
                    f"Executor did not enter reconnecting state after Control loss: {current}"
                )
                await asyncio.sleep(0.25)

            control = start_control_process(
                control_config,
                env=isolated_xdg_env(root / "xdg"),
                stdout_path=root / "control.out",
                stderr_path=root / "control.err",
            )
            await wait_for_http_ready(
                url,
                control,
                stdout_path=root / "control.out",
                stderr_path=root / "control.err",
                timeout_s=35,
            )
            first_seen = await wait_for_executor_online(
                url,
                control,
                executor_id,
                after_last_seen=first_seen,
                timeout_s=55,
                stdout_path=root / "control.out",
                stderr_path=root / "control.err",
            )
            # Control sees Hello before the Executor can persist its local
            # connected snapshot; allow that short cross-process ordering gap.
            deadline = asyncio.get_running_loop().time() + 10
            while not connection.describe().startswith("connected;"):
                current = connection.describe()
                assert not current.startswith("action-required"), current
                assert asyncio.get_running_loop().time() < deadline, (
                    f"Executor did not report recovery: {current}"
                )
                await asyncio.sleep(0.25)
            assert task.status() == original_task
            assert _task_last_run(task.name) == original_start
            assert profile_path.read_bytes() == original_profile

            # Existing explicit stop/restart test remains in the full CI smoke.
            if not outage_only:
                task.stop()
                deadline = asyncio.get_running_loop().time() + 15
                while task.status()["state"].lower() in {"running", "queued"}:
                    assert asyncio.get_running_loop().time() < deadline, (
                        "Task Scheduler did not stop the Executor"
                    )
                    await asyncio.sleep(0.2)
                task.start()
                await wait_for_executor_online(
                    url,
                    control,
                    executor_id,
                    after_last_seen=first_seen,
                    timeout_s=55,
                    stdout_path=root / "control.out",
                    stderr_path=root / "control.err",
                )
                assert task.status()["state"].lower() == "running"
        except Exception:
            error_file = root / "executor-error.log"
            diagnostic = (
                error_file.read_text(encoding="utf-8", errors="replace")[-6000:]
                if error_file.exists()
                else "No Executor errors"
            )
            print(
                diagnostic.encode("ascii", "backslashreplace").decode("ascii")
            )
            raise
    finally:
        try:
            task.uninstall()
            assert task.status() is None
        finally:
            if control is not None:
                stop_process(control)
            for _ in range(30):
                try:
                    shutil.rmtree(root)
                    break
                except PermissionError:
                    time.sleep(0.2)
            else:
                shutil.rmtree(
                    root
                )  # Do not report success if cleanup never finishes.
    print(
        "Native Windows paired Task Scheduler Control outage/recovery "
        "without task restart and with identity preservation: PASS"
    )
    if not outage_only:
        print("Native Windows paired Task Scheduler explicit restart: PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--outage-only", action="store_true")
    asyncio.run(main(outage_only=parser.parse_args().outage_only))
