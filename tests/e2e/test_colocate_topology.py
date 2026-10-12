"""Colocate runs ordinary, pre-paired Control and Executor children."""

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.e2e.helpers import (
    free_tcp_port,
    isolated_process_env,
    isolated_xdg_env,
    provision_executor_pair,
    stop_process,
    wait_for_executor_online,
    wait_for_http_ready,
    write_yaml_config,
)

pytestmark = [pytest.mark.integration, pytest.mark.topology]


@pytest.mark.asyncio
async def test_colocate_reuses_normal_role_state_and_authenticated_executor(
    tmp_path: Path,
) -> None:
    port = free_tcp_port()
    control_url = f"http://127.0.0.1:{port}"
    control_state = tmp_path / "control-state"
    executor_state = tmp_path / "executor-state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor_id = provision_executor_pair(
        control_state_dir=control_state,
        executor_state_dir=executor_state,
        control_url=control_url,
    )
    config_root = tmp_path / "configs"
    control_config = write_yaml_config(
        config_root / "control.yaml",
        {
            "mode": "http",
            "host": "127.0.0.1",
            "port": port,
            "auth_mode": "none",
            "state_dir": str(control_state),
            "data_dir": str(tmp_path / "data"),
            "agent_bridge_enabled": False,
        },
    )
    executor_config = write_yaml_config(
        config_root / "executor.yaml",
        {"state_dir": str(executor_state), "default_workdir": str(workspace)},
    )
    stdout_path = tmp_path / "colocate.stdout"
    stderr_path = tmp_path / "colocate.stderr"
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        launcher = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "workgate.main",
                "colocate",
                "--control-config",
                str(control_config),
                "--executor-config",
                str(executor_config),
            ],
            env=isolated_process_env(isolated_xdg_env(tmp_path / "xdg")),
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        try:
            await wait_for_http_ready(
                control_url,
                launcher,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            await wait_for_executor_online(
                control_url,
                launcher,
                executor_id,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.get(f"{control_url}/api/ui/executors")
            response.raise_for_status()
            assert any(
                row["executor_id"] == executor_id and row["online"]
                for row in response.json()["data"]["executors"]
            )
        finally:
            stop_process(launcher)

    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"{control_url}/healthz")

    from workgate.persistence import FileStateStore

    assert FileStateStore(
        lambda: executor_state
    ).layout.executor_profile_path.exists()
