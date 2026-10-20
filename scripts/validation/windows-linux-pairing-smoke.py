"""Manual WSL/Linux Control <-> native Windows Executor real pairing smoke.

Run on a WSL Linux host with working Windows interop and a native Windows
Python environment with Workgate dependencies already installed.
"""

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.helpers import (
    free_tcp_port,
    isolated_xdg_env,
    start_control_process,
    stop_process,
    wait_for_executor_online,
    wait_for_http_ready,
    write_yaml_config,
)


def windows_path(path: Path) -> str:
    return subprocess.check_output(
        ["wslpath", "-w", str(path)], text=True
    ).strip()


async def run(*, python_exe: Path, windows_temp: Path) -> None:
    assert (
        sys.platform == "linux"
        and Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()
    ), "Run on WSL with Windows interop enabled"
    assert python_exe.is_file() and windows_temp.is_dir()
    native = subprocess.check_output(
        [str(python_exe), "-c", "import sys; print(sys.platform)"],
        text=True,
        timeout=15,
    ).strip()
    assert native == "win32", (
        "Executor test must use a native Windows interpreter"
    )
    root = Path(tempfile.mkdtemp(prefix="workgate-crossos-", dir=windows_temp))
    linux_root = Path(tempfile.mkdtemp(prefix="workgate-crossos-linux-"))
    control = None
    connect = None
    executor = None
    try:
        src = root / "src"
        shutil.copytree(Path(__file__).resolve().parents[2] / "src", src)
        win_root = windows_path(root)
        win_src = windows_path(src)
        win_state = win_root + "\\executor-state"
        win_workspace = win_root + "\\workspace with spaces"
        (root / "workspace with spaces").mkdir()
        win_config = write_yaml_config(
            root / "windows-executor.yaml",
            {
                "state_dir": win_state,
                "default_workdir": win_workspace,
                "shell_executable": "powershell.exe",
            },
        )
        env = os.environ.copy()
        env.update(
            PYTHONPATH=win_src,
            APPDATA=win_root + "\\appdata",
            LOCALAPPDATA=win_root + "\\localappdata",
            USERPROFILE=win_root,
            HOME=win_root,
        )
        (root / "appdata").mkdir()
        (root / "localappdata").mkdir()
        port = free_tcp_port()
        url = f"http://127.0.0.1:{port}"
        control_config = write_yaml_config(
            linux_root / "linux-control.yaml",
            {
                "mode": "http",
                "host": "127.0.0.1",
                "port": port,
                "base_url": url,
                "auth_mode": "none",
                "state_dir": str(linux_root / "control-state"),
                "data_dir": str(linux_root / "control-data"),
                "agent_bridge_enabled": False,
            },
        )
        control = start_control_process(
            control_config,
            env=isolated_xdg_env(linux_root / "xdg"),
            stdout_path=linux_root / "control.out",
            stderr_path=linux_root / "control.err",
        )
        await wait_for_http_ready(
            url,
            control,
            stdout_path=linux_root / "control.out",
            stderr_path=linux_root / "control.err",
            timeout_s=35,
        )
        python_cmd = [str(python_exe), "-m", "workgate.main", "executor"]
        with (
            (root / "connect.out").open("w") as stdout,
            (root / "connect.err").open("w") as stderr,
        ):
            connect = subprocess.Popen(
                [
                    *python_cmd,
                    "connect",
                    url,
                    "--name",
                    "Windows-native-to-Linux-Control",
                    "--config",
                    windows_path(win_config),
                ],
                cwd=root,
                env=env,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
        with httpx.Client(timeout=3, trust_env=False) as client:
            deadline = time.monotonic() + 50
            code = None
            while time.monotonic() < deadline:
                if connect.poll() is not None:
                    raise AssertionError(
                        f"native Windows connect exited {connect.returncode}: "
                        + (root / "connect.err").read_text(errors="replace")[
                            -2000:
                        ]
                    )
                match = re.search(
                    r"(?m)^Code: (\S+)$",
                    (root / "connect.out").read_text(errors="replace"),
                )
                if match:
                    code = match.group(1)
                    break
                await asyncio.sleep(0.1)
            assert code, (
                "native Windows CLI did not publish a device code to Linux Control"
            )
            detail = client.get(f"{url}/api/ui/pair", params={"code": code})
            assert detail.status_code == 200, detail.text
            assert (
                detail.json()["data"]["requested_name"]
                == "Windows-native-to-Linux-Control"
            )
            approved = client.post(
                f"{url}/api/ui/pair",
                json={
                    "user_code": code,
                    "decision": "approve",
                    "name": "cross-os-approved",
                },
            )
            assert approved.status_code == 200, approved.text
            executor_id = approved.json()["data"]["executor_id"]
        assert connect.wait(timeout=40) == 0, (root / "connect.err").read_text(
            errors="replace"
        )
        with (
            (root / "executor.out").open("w") as stdout,
            (root / "executor.err").open("w") as stderr,
        ):
            executor = subprocess.Popen(
                [*python_cmd, "run", "--config", windows_path(win_config)],
                cwd=root,
                env=env,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
        first_seen = await wait_for_executor_online(
            url,
            control,
            executor_id,
            timeout_s=50,
            stdout_path=linux_root / "control.out",
            stderr_path=linux_root / "control.err",
        )
        stop_process(executor)
        executor = None
        with (
            (root / "executor-restarted.out").open("w") as stdout,
            (root / "executor-restarted.err").open("w") as stderr,
        ):
            executor = subprocess.Popen(
                [*python_cmd, "run", "--config", windows_path(win_config)],
                cwd=root,
                env=env,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
        await wait_for_executor_online(
            url,
            control,
            executor_id,
            after_last_seen=first_seen,
            timeout_s=55,
            stdout_path=linux_root / "control.out",
            stderr_path=linux_root / "control.err",
        )
    finally:
        try:
            for process in (executor, connect, control):
                if process is not None:
                    stop_process(process)
        finally:
            try:
                shutil.rmtree(linux_root)
            finally:
                for attempt in range(40):
                    try:
                        shutil.rmtree(root)
                        break
                    except PermissionError:
                        if attempt == 39:
                            raise
                        await asyncio.sleep(0.2)
    print(
        "WSL/Linux Control <-> native Windows Executor owner-approved reconnect: PASS"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-python", type=Path, required=True)
    parser.add_argument("--windows-temp", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(
        run(python_exe=args.windows_python, windows_temp=args.windows_temp)
    )
