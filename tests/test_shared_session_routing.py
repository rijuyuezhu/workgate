from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tests.helpers import mcp_text
from workgate.config.settings import Settings, clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp
from workgate.control.runtime import build_control_runtime
from workgate.control.state import ExecutorTrustRecord
from workgate.executor.connection import ExecutorConnection
from workgate.executor.control_client import ExecutorControlClient
from workgate.executor.hello import build_executor_hello
from workgate.executor.profile import ExecutorProfile
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.ids import new_executor_id


def _mcp_data(response: Any) -> dict[str, Any]:
    data = (
        response[1]
        if isinstance(response, tuple)
        else json.loads(mcp_text(response))
    )
    assert isinstance(data, dict)
    return data


async def _wait_executor_online(control, executor_id: str) -> None:
    async def online() -> None:
        while not await control.executor_transport.is_online(executor_id):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(online(), timeout=1.0)


@pytest.mark.asyncio
async def test_same_machine_execution_crosses_loopback_and_never_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_workspace = tmp_path / "control-workspace"
    executor_workspace = tmp_path / "executor-workspace"
    control_workspace.mkdir()
    executor_workspace.mkdir()
    # The final deployment uses separate processes, so each side gets its own
    # ambient compatibility settings. This in-process protocol test switches
    # the ambient bridge to the executor while it is alive, then back to control
    # after the simulated executor exit.
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(executor_workspace))
    clear_settings_cache()

    control = build_control_runtime(
        Settings(
            workspace_root=control_workspace,
            state_dir=tmp_path / "control-state",
            auth_mode="none",
            remote_enabled=False,
            agent_bridge_enabled=False,
        )
    )
    executor = build_executor_runtime(
        Settings(
            workspace_root=executor_workspace,
            state_dir=tmp_path / "executor-state",
            remote_enabled=False,
            agent_bridge_enabled=False,
        ),
        enable_control_connection=False,
    )
    executor_id = new_executor_id()
    credential = new_executor_credential()
    clock = {"now": 100.0}
    control.executor_transport._clock = lambda: clock["now"]

    await control.start()
    await executor.start()
    executor_closed = False
    http_client: httpx.AsyncClient | None = None
    connection: ExecutorConnection | None = None
    try:
        control.control_state.put_executor(
            ExecutorTrustRecord(
                executor_id=executor_id,
                name="loopback-executor",
                credential_verifier=executor_credential_verifier(credential),
                created_at=1,
            )
        )
        app = build_http_app(runtime=control)
        profile = ExecutorProfile(
            control_url="http://127.0.0.1",
            executor_id=executor_id,
            credential=credential,
        )
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=profile.control_url,
            headers={"Authorization": f"Bearer {credential}"},
        )
        client = ExecutorControlClient(profile, client=http_client)
        connection = ExecutorConnection.from_client(
            client,
            hello_factory=lambda: build_executor_hello(
                executor.config, sessions=executor.sessions.inventory()
            ),
            execute=executor._execute_protocol_command,
            max_concurrent_commands=executor.config.max_concurrent_commands,
        )
        connection.start()
        await _wait_executor_online(control, executor_id)

        mcp = build_mcp(runtime=control)
        started = _mcp_data(
            await mcp.call_tool("session_start", {"workdir": "."})
        )
        session_id = started["session_id"]
        assert started["executor_id"] == executor_id

        destination = _mcp_data(
            await mcp.call_tool("session_start", {"workdir": "."})
        )
        dst_session_id = destination["session_id"]
        assert destination["executor_id"] == executor_id

        await mcp.call_tool(
            "write_file",
            {
                "session_id": session_id,
                "path": "executor-only.txt",
                "content": "crossed executor protocol\n",
            },
        )
        assert (executor_workspace / "executor-only.txt").read_text() == (
            "crossed executor protocol\n"
        )
        assert not (control_workspace / "executor-only.txt").exists()

        background = _mcp_data(
            await mcp.call_tool(
                "session_copy",
                {
                    "src_session_id": session_id,
                    "src_path": "executor-only.txt",
                    "dst_session_id": dst_session_id,
                    "dst_path": "copied-in-background.txt",
                    "background": True,
                },
            )
        )
        background_result = background["result"]
        assert isinstance(background_result, dict)
        job_id = background_result["job_id"]
        job_row: dict[str, Any] | None = None
        for _ in range(100):
            polled = _mcp_data(
                await mcp.call_tool(
                    "job",
                    {"session_id": session_id, "poll": [job_id]},
                )
            )
            outputs = polled.get("outputs", [])
            if outputs:
                candidate = outputs[0]["job"]
                assert isinstance(candidate, dict)
                job_row = candidate
                if candidate["status"] in {
                    "succeeded",
                    "failed",
                    "stopped",
                    "lost",
                }:
                    break
            await asyncio.sleep(0.01)
        assert job_row is not None
        assert job_row["status"] == "succeeded", job_row
        assert (
            executor_workspace / "copied-in-background.txt"
        ).read_text() == ("crossed executor protocol\n")
        assert not (control_workspace / "copied-in-background.txt").exists()
        assert not hasattr(control.services, "tool_session_store")

        await connection.aclose()
        connection = None
        await executor.aclose()
        executor_closed = True
        monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(control_workspace))
        clear_settings_cache()
        clock["now"] = 1_000.0
        assert not await control.executor_transport.is_online(executor_id)

        offline_job = _mcp_data(
            await mcp.call_tool(
                "job",
                {"session_id": session_id, "poll": [job_id]},
            )
        )
        assert offline_job["outputs"][0]["job"]["status"] == "succeeded"

        with pytest.raises(ToolError, match="executor is offline"):
            await mcp.call_tool(
                "write_file",
                {
                    "session_id": session_id,
                    "path": "must-not-fallback.txt",
                    "content": "forbidden\n",
                },
            )
        assert not (executor_workspace / "must-not-fallback.txt").exists()
        assert not (control_workspace / "must-not-fallback.txt").exists()
    finally:
        if connection is not None:
            await connection.aclose()
        if http_client is not None:
            await http_client.aclose()
        if not executor_closed:
            await executor.aclose()
        await control.aclose()
