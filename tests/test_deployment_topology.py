import os
from pathlib import Path

import httpx
import pytest

from tests.e2e_helpers import (
    RestToolClient,
    free_tcp_port,
    isolated_xdg_env,
    provision_executor_pair,
    start_control_process,
    start_executor_process,
    stop_process,
    wait_for_executor_online,
    wait_for_http_ready,
    write_yaml_config,
)
from workgate.persistence import FileStateStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.topology,
    pytest.mark.skipif(
        os.name == "nt",
        reason="deployment topology process acceptance runs on POSIX CI",
    ),
]


async def _executor_target(base_url: str, executor_id: str) -> dict:
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        response = await client.get(f"{base_url}/api/ui/bootstrap")
    response.raise_for_status()
    rows = response.json()["data"]["executor_targets"]
    return next(row for row in rows if row["executor_id"] == executor_id)


@pytest.mark.asyncio
async def test_separate_process_topology_reuses_identity_and_rehydrates_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("topology\n", encoding="utf-8")
    control_state = tmp_path / "control-state"
    control_data = tmp_path / "control-data"
    executor_state = tmp_path / "executor-state"
    config_dir = tmp_path / "config"
    logs = tmp_path / "logs"
    process_env = isolated_xdg_env(tmp_path / "xdg")

    # Poison ambient settings: child processes must use only their explicit YAML.
    wrong_state = tmp_path / "wrong-state"
    wrong_workspace = tmp_path / "wrong-workspace"
    monkeypatch.setenv("WORKGATE_PORT", "1")
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(wrong_state))
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(wrong_workspace))

    executor_id = provision_executor_pair(
        control_state_dir=control_state,
        executor_state_dir=executor_state,
        control_url=base_url,
        name="topology-executor",
    )
    control_config = write_yaml_config(
        config_dir / "control.yaml",
        {
            "mode": "http",
            "host": "127.0.0.1",
            "port": port,
            "base_url": base_url,
            "auth_mode": "none",
            "state_dir": str(control_state),
            "data_dir": str(control_data),
            "agent_bridge_enabled": False,
            "tool_timeout_s": 15,
        },
    )
    executor_config = write_yaml_config(
        config_dir / "executor.yaml",
        {
            "workspace_root": str(workspace),
            "state_dir": str(executor_state),
            "run_shell_default_timeout_s": 5,
            "run_shell_max_timeout_s": 10,
        },
    )
    profile_path = FileStateStore(
        lambda: executor_state
    ).layout.executor_profile_path
    profile_before = profile_path.read_bytes()

    control = start_control_process(
        control_config,
        env=process_env,
        stdout_path=logs / "control-1.stdout.log",
        stderr_path=logs / "control-1.stderr.log",
    )
    executor = None
    restarted_executor = None
    restarted_control = None
    try:
        await wait_for_http_ready(
            base_url,
            control,
            stdout_path=logs / "control-1.stdout.log",
            stderr_path=logs / "control-1.stderr.log",
        )

        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            unavailable = await client.post(
                f"{base_url}/tools/session_start",
                json={"workdir": "."},
            )
        assert unavailable.status_code >= 400
        assert (
            "session_start requires exactly one eligible executor or an explicit executor_id"
            in unavailable.text
        )

        executor = start_executor_process(
            executor_config,
            env=process_env,
            stdout_path=logs / "executor-1.stdout.log",
            stderr_path=logs / "executor-1.stderr.log",
        )
        await wait_for_executor_online(
            base_url,
            executor,
            executor_id,
            stdout_path=logs / "executor-1.stdout.log",
            stderr_path=logs / "executor-1.stderr.log",
        )

        client = RestToolClient(base_url)
        session = await client.call_tool(
            "session_start",
            {"workdir": ".", "executor_id": executor_id},
        )
        session_id = session["session_id"]
        first_read = await client.call_tool(
            "read", {"session_id": session_id, "path": "marker.txt"}
        )
        assert "topology" in first_read["content"]

        initial_target = await _executor_target(base_url, executor_id)
        assert initial_target["status"] == "online"
        assert initial_target["workspace_root"] == str(workspace)
        seen_before_restart = initial_target["last_seen_at"]
        assert isinstance(seen_before_restart, int | float)
        stop_process(executor)
        restarted_executor = start_executor_process(
            executor_config,
            env=process_env,
            stdout_path=logs / "executor-2.stdout.log",
            stderr_path=logs / "executor-2.stderr.log",
        )
        fresh_hello_seen = await wait_for_executor_online(
            base_url,
            restarted_executor,
            executor_id,
            after_last_seen=seen_before_restart,
            stdout_path=logs / "executor-2.stdout.log",
            stderr_path=logs / "executor-2.stderr.log",
            timeout_s=20,
        )
        # A fresh hello publishes presence. Wait for later authenticated
        # activity before the read below proves that command delivery recovered.
        await wait_for_executor_online(
            base_url,
            restarted_executor,
            executor_id,
            after_last_seen=fresh_hello_seen,
            stdout_path=logs / "executor-2.stdout.log",
            stderr_path=logs / "executor-2.stderr.log",
            timeout_s=20,
        )
        assert profile_path.read_bytes() == profile_before
        reconnected_target = await _executor_target(base_url, executor_id)
        assert reconnected_target["status"] == "online"
        assert reconnected_target["workspace_root"] == str(workspace)

        after_executor_restart = await client.call_tool(
            "read", {"session_id": session_id, "path": "marker.txt"}
        )
        assert "topology" in after_executor_restart["content"]

        stop_process(control)
        restarted_control = start_control_process(
            control_config,
            env=process_env,
            stdout_path=logs / "control-2.stdout.log",
            stderr_path=logs / "control-2.stderr.log",
        )
        await wait_for_http_ready(
            base_url,
            restarted_control,
            stdout_path=logs / "control-2.stdout.log",
            stderr_path=logs / "control-2.stderr.log",
        )
        await wait_for_executor_online(
            base_url,
            restarted_executor,
            executor_id,
            stdout_path=logs / "executor-2.stdout.log",
            stderr_path=logs / "executor-2.stderr.log",
            timeout_s=30,
        )
        assert profile_path.read_bytes() == profile_before
        rehydrated_target = await _executor_target(base_url, executor_id)
        assert rehydrated_target["status"] == "online"
        assert rehydrated_target["workspace_root"] == str(workspace)

        after_control_restart = await client.call_tool(
            "read", {"session_id": session_id, "path": "marker.txt"}
        )
        assert "topology" in after_control_restart["content"]
        assert not wrong_state.exists()
        assert not wrong_workspace.exists()
    finally:
        for process in (
            restarted_executor,
            executor,
            restarted_control,
            control,
        ):
            if process is not None:
                stop_process(process)
