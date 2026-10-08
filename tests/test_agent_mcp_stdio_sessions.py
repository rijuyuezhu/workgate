"""Real stdio MCP child lifecycle and executor workdir isolation regressions."""

import json
import os
import sys
from pathlib import Path

import pytest

from tests.helpers import build_paired_control_harness, build_tool_session_store
from workgate.config.executor import ExecutorConfig, resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.agent import ExecutorAgentBridgeService
from workgate.executor.tool_session.store import UnknownAgentSessionError

_SERVER_SOURCE = """import os
import uuid
from mcp.server.fastmcp import FastMCP

app = FastMCP('stdio-isolation-test')
instance_id = uuid.uuid4().hex

@app.tool()
def inspect() -> dict[str, str | int]:
    return {'cwd': os.getcwd(), 'pid': os.getpid(), 'instance': instance_id}

app.run(transport='stdio')
"""


def _install_stdio_server(config: ExecutorConfig, tmp_path: Path) -> None:
    script = tmp_path / "stdio_server.py"
    script.write_text(_SERVER_SOURCE, encoding="utf-8")
    config.agent_config_dir.mkdir(parents=True, exist_ok=True)
    (config.agent_config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "local": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(script)],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_stdio_tools_use_selected_session_cwd_and_new_process_each_time(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    first_dir = root / "first"
    second_dir = root / "second"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    settings = Settings(
        default_workdir=root,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=True,
        agent_mcp_probe_timeout_s=15,
        agent_mcp_call_timeout_s=20,
    )
    config = resolve_executor_config(settings)
    _install_stdio_server(config, tmp_path)
    store = build_tool_session_store(settings)
    first_session = "sess_0000000000000000000001"
    second_session = "sess_0000000000000000000002"
    store.create_session(session_id=first_session, workdir=first_dir)
    store.create_session(session_id=second_session, workdir=second_dir)
    bridge = ExecutorAgentBridgeService(config, store)

    assert "local" in bridge.list_servers(first_session).root
    tools = bridge.list_tools(second_session, "local")
    assert any(row["tool"] == "inspect" for row in tools.tools)

    first = await bridge.call_tool(first_session, "local", "inspect")
    second = await bridge.call_tool(second_session, "local", "inspect")
    again = await bridge.call_tool(first_session, "local", "inspect")

    def details(response):
        data = response.model_dump(mode="json")
        return data["structured_content"]

    one, two, three = map(details, (first, second, again))
    assert one["cwd"] == str(first_dir)
    assert two["cwd"] == str(second_dir)
    assert three["cwd"] == str(first_dir)
    assert len({one["instance"], two["instance"], three["instance"]}) == 3
    if os.name != "nt":
        for pid in (one["pid"], two["pid"], three["pid"]):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)

    third_dir = root / "changed"
    third_dir.mkdir()
    store.change_session_workdir(first_session, third_dir)
    moved = details(await bridge.call_tool(first_session, "local", "inspect"))
    assert moved["cwd"] == str(third_dir)
    assert moved["instance"] not in {
        one["instance"],
        two["instance"],
        three["instance"],
    }

    with pytest.raises(UnknownAgentSessionError):
        await bridge.call_tool("sess_missing", "local", "inspect")


@pytest.mark.asyncio
async def test_stdio_routes_from_control_to_selected_executor_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    first_dir = root / "alpha"
    second_dir = root / "beta"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    settings = Settings(
        default_workdir=root,
        state_dir=tmp_path / "control-state",
        agent_bridge_enabled=True,
        agent_mcp_probe_timeout_s=15,
        agent_mcp_call_timeout_s=20,
    )
    harness = build_paired_control_harness(settings)
    _install_stdio_server(harness.executor.config, tmp_path)
    try:
        sessions = harness.control.session_coordinator
        first = await sessions.start_session(
            workdir=str(first_dir), executor_id=harness.executor_id
        )
        second = await sessions.start_session(
            workdir=str(second_dir), executor_id=harness.executor_id
        )
        assert isinstance(first, dict) and isinstance(second, dict)
        first_id = str(first["session_id"])
        second_id = str(second["session_id"])
        for sid, expected in ((first_id, first_dir), (second_id, second_dir)):
            result = await sessions.call_session_tool(
                "agent_mcp.call_tool",
                {
                    "session_id": sid,
                    "server": "local",
                    "tool": "inspect",
                    "args": {},
                },
            )
            assert isinstance(result, dict)
            structured = result["structured_content"]
            assert isinstance(structured, dict)
            assert structured["cwd"] == str(expected)
        with pytest.raises(ValueError, match="session_id"):
            await sessions.call_session_tool(
                "agent_mcp.call_tool", {"server": "local", "tool": "inspect"}
            )
        await sessions.end_session(first_id)
        await sessions.end_session(second_id)
    finally:
        await harness.executor.aclose()
        await harness.control.aclose()
