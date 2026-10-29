"""Disposable real Windows frozen a11 -> source -> frozen native-task migration."""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Native Windows Python is required")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tests.e2e.helpers import (  # noqa: E402
    DebugHttpToolClient,
    free_tcp_port,
    isolated_xdg_env,
    provision_executor_pair,
    stop_process,
    wait_for_executor_online,
    wait_for_http_ready,
    write_yaml_config,
)
from workgate.persistence import FileStateStore  # noqa: E402
from workgate.platform.windows_task import WindowsUserTask  # noqa: E402

TASK = WindowsUserTask(
    "Workgate Executor",
    description="Managed by Workgate: executor",
    run=lambda argv: subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=35
    ),
)


def cli(
    executable: list[str], action: str, config: Path, env: dict[str, str]
) -> str:
    result = subprocess.run(
        [*executable, "executor", action, "--config", str(config)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=85,
    )
    return result.stdout


def control(
    executable: list[str],
    config: Path,
    root: Path,
    env: dict[str, str],
    label: str,
) -> subprocess.Popen[str]:
    out = root / f"{label}-control.out"
    err = root / f"{label}-control.err"
    with out.open("w") as stdout, err.open("w") as stderr:
        return subprocess.Popen(
            [*executable, "control", "--config", str(config)],
            env=env,
            stdout=stdout,
            stderr=stderr,
        )


def stop_control(process: subprocess.Popen[str]) -> None:
    # A frozen Windows Control has a supervisor child; terminate our whole
    # disposable process tree so it cannot retain the old Control state lock.
    if process.poll() is None:
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            check=True,
            capture_output=True,
            timeout=25,
        )
        process.wait(timeout=20)
    else:
        stop_process(process)


async def online(
    base_url: str,
    process: subprocess.Popen[str],
    executor_id: str,
    root: Path,
    label: str,
) -> None:
    await wait_for_executor_online(
        base_url,
        process,
        executor_id,
        timeout_s=65,
        stdout_path=root / f"{label}-control.out",
        stderr_path=root / f"{label}-control.err",
    )
    client = DebugHttpToolClient(base_url)
    session = await client.call_tool(
        "session_start", {"workdir": ".", "executor_id": executor_id}
    )
    response = await client.call_tool(
        "read", {"session_id": session["session_id"], "path": "marker.txt"}
    )
    assert "frozen-to-source-windows" in response["content"]


async def main(old_exe: Path) -> None:
    assert old_exe.is_file() and old_exe.with_name("workgatew.exe").is_file()
    old = [str(old_exe)]
    new = [sys.executable, "-m", "workgate.main"]
    assert "5.0.0a11" in subprocess.check_output([*old, "--version"], text=True)
    assert TASK.status() is None, (
        "do not replace an existing Workgate Executor task"
    )
    root = Path(tempfile.gettempdir()) / f"wg457-native-migration-{os.getpid()}"
    root.mkdir()
    process = None
    try:
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "marker.txt").write_text("frozen-to-source-windows\n")
        port = free_tcp_port()
        base_url = f"http://127.0.0.1:{port}"
        control_state = root / "control-state"
        executor_state = root / "executor-state"
        executor_id = provision_executor_pair(
            control_state_dir=control_state,
            executor_state_dir=executor_state,
            control_url=base_url,
        )
        control_config = write_yaml_config(
            root / "control.yaml",
            {
                "mode": "http",
                "host": "127.0.0.1",
                "port": port,
                "base_url": base_url,
                "auth_mode": "none",
                "state_dir": str(control_state),
                "data_dir": str(root / "control-data"),
                "agent_bridge_enabled": False,
            },
        )
        executor_config = write_yaml_config(
            root / "executor.yaml",
            {
                "state_dir": str(executor_state),
                "default_workdir": str(workspace),
            },
        )
        profile = FileStateStore(
            lambda: executor_state
        ).layout.executor_profile_path
        original_profile = profile.read_bytes()

        # Task Scheduler's limited token must be able to read the private test state.
        account = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        subprocess.run(
            ["icacls.exe", str(root), "/grant", f"{account}:(OI)(CI)F", "/T"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        env = {**os.environ, **isolated_xdg_env(root / "xdg")}
        env.pop("PYTHONPATH", None)

        async def stage(
            label: str, executable: list[str], expected: str
        ) -> None:
            nonlocal process
            process = control(executable, control_config, root, env, label)
            await wait_for_http_ready(
                base_url,
                process,
                timeout_s=45,
                stdout_path=root / f"{label}-control.out",
                stderr_path=root / f"{label}-control.err",
            )
            assert "State: running" in cli(
                executable, "install-service", executor_config, env
            )
            status = cli(executable, "status", executor_config, env)
            assert f"Runtime owner: {expected}" in status, status
            task = TASK.status()
            assert task is not None and task["state"].lower() == "running"
            action = task["action"]
            expected_image = (
                old_exe.with_name("workgatew.exe")
                if expected == "self-contained"
                else Path(sys.executable).with_name("pythonw.exe")
            )
            assert Path(action["execute"]) == expected_image
            await online(base_url, process, executor_id, root, label)
            assert profile.read_bytes() == original_profile
            print(
                f"{label}: same Executor ID, Task Scheduler owner, remote read: PASS",
                flush=True,
            )

        async def stop(executable: list[str]) -> None:
            nonlocal process
            cli(executable, "stop", executor_config, env)
            cli(executable, "uninstall-service", executor_config, env)
            assert TASK.status() is None
            assert profile.read_bytes() == original_profile
            assert process is not None
            stop_control(process)
            process = None
            await asyncio.sleep(0.4)

        await stage("frozen-before", old, "self-contained")
        await stop(old)
        await stage("managed-source", new, "source")
        await stop(new)
        await stage("frozen-rollback", old, "self-contained")
        await stop(old)
        print(
            "Real Windows frozen -> source -> frozen rollback: PASS", flush=True
        )
    finally:
        if process is not None:
            stop_control(process)
        # This test only owns the task when it was absent at entry.
        if TASK.status() is not None:
            TASK.uninstall()
        for _ in range(30):
            try:
                shutil.rmtree(root)
                break
            except PermissionError:
                time.sleep(0.2)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: windows-frozen-migration-smoke.py <old-workgate.exe>"
        )
    asyncio.run(main(Path(sys.argv[1]).resolve()))
