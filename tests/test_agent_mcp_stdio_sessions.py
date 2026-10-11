"""Real stdio MCP child lifecycle and executor workdir isolation regressions."""

import json
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import workgate.agent_bridge.mcp as agent_mcp_module
from tests.helpers import build_paired_control_harness, build_tool_session_store
from workgate.config.control import resolve_control_config
from workgate.config.executor import ExecutorConfig, resolve_executor_config
from workgate.config.settings import Settings
from workgate.control.agent_bridge import ControlAgentBridgeService
from workgate.executor.agent import ExecutorAgentBridgeService
from workgate.executor.tool_session.store import UnknownAgentSessionError

_SERVER_SOURCE = """import os
import uuid
from mcp.server.mcpserver import MCPServer

app = MCPServer('stdio-isolation-test')
instance_id = uuid.uuid4().hex

@app.tool()
def inspect() -> dict[str, str | int]:
    return {'cwd': os.getcwd(), 'pid': os.getpid(), 'instance': instance_id}

app.run(transport='stdio')
"""


def _install_stdio_server(
    config: ExecutorConfig,
    tmp_path: Path,
    names: tuple[str, ...] = ("local",),
) -> None:
    script = tmp_path / "stdio_server.py"
    script.write_text(_SERVER_SOURCE, encoding="utf-8")
    config.agent_config_dir.mkdir(parents=True, exist_ok=True)
    (config.agent_config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    name: {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(script)],
                    }
                    for name in names
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


@pytest.mark.asyncio
async def test_routed_call_probes_no_servers_and_starts_only_target_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workdir"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "control-state",
        agent_bridge_enabled=True,
        agent_mcp_probe_timeout_s=15,
        agent_mcp_call_timeout_s=20,
    )
    harness = build_paired_control_harness(settings)
    _install_stdio_server(harness.executor.config, tmp_path, ("local", "other"))
    launches: list[str] = []
    original_stdio_client = agent_mcp_module.stdio_client

    @asynccontextmanager
    async def counted_stdio_client(params):
        launches.append(params.command)
        async with original_stdio_client(params) as streams:
            yield streams

    monkeypatch.setattr(agent_mcp_module, "stdio_client", counted_stdio_client)
    service = ControlAgentBridgeService(
        resolve_control_config(settings), harness.control.session_coordinator
    )
    loop_thread = threading.get_ident()
    original_network_registry = service._network_registry

    def observed_network_registry(
        *, probe_mcp_tools=True, mcp_server_name=None
    ):
        if probe_mcp_tools:
            assert threading.get_ident() != loop_thread
        return original_network_registry(
            probe_mcp_tools=probe_mcp_tools, mcp_server_name=mcp_server_name
        )

    monkeypatch.setattr(service, "_network_registry", observed_network_registry)
    original_executor_registry = harness.executor.agent_bridge._registry

    def observed_executor_registry(
        session_id, *, probe_mcp_tools=True, mcp_server_name=None
    ):
        if probe_mcp_tools:
            assert threading.get_ident() != loop_thread
        return original_executor_registry(
            session_id,
            probe_mcp_tools=probe_mcp_tools,
            mcp_server_name=mcp_server_name,
        )

    monkeypatch.setattr(
        harness.executor.agent_bridge, "_registry", observed_executor_registry
    )

    try:
        created = await harness.control.session_coordinator.start_session(
            workdir=str(workspace), executor_id=harness.executor_id
        )
        assert isinstance(created, dict)
        session_id = str(created["session_id"])
        result = await service.call_tool("local", "inspect", {}, session_id)
        assert result.model_dump(mode="json")["structured_content"][
            "cwd"
        ] == str(workspace)
        assert len(launches) == 1

        # Explicit status/metadata discovery still performs real probes.
        servers = await service.list_servers(session_id)
        assert servers.root["local"]["available"] is True
        assert servers.root["other"]["available"] is True
        assert len(launches) == 3
        await harness.control.session_coordinator.end_session(session_id)
    finally:
        await harness.executor.aclose()
        await harness.control.aclose()


@pytest.mark.asyncio
async def test_scoped_discovery_probes_only_selected_stdio_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workdir"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=True,
        agent_mcp_probe_timeout_s=15,
        agent_mcp_call_timeout_s=20,
    )
    harness = build_paired_control_harness(settings)
    _install_stdio_server(
        harness.executor.config, tmp_path, ("first", "second", "third")
    )
    launches: list[str] = []
    original_stdio = agent_mcp_module.stdio_client

    @asynccontextmanager
    async def counted_stdio(params):
        launches.append(params.command)
        async with original_stdio(params) as streams:
            yield streams

    monkeypatch.setattr(agent_mcp_module, "stdio_client", counted_stdio)
    bridge = ControlAgentBridgeService(
        resolve_control_config(settings), harness.control.session_coordinator
    )
    try:
        created = await harness.control.session_coordinator.start_session(
            workdir=str(workspace), executor_id=harness.executor_id
        )
        assert isinstance(created, dict)
        sid = str(created["session_id"])
        match = await bridge.search_tools(
            "inspect", session_id=sid, server="first"
        )
        assert match["total_matches"] == 1
        assert match["tools"][0]["server"] == "first"
        assert len(launches) == 1
        assert (await bridge.inspect_tool("first", "inspect", session_id=sid))[
            "tool"
        ] == "inspect"
        assert len(launches) == 1
        await bridge.search_tools("inspect", session_id=sid, server="second")
        assert len(launches) == 2
        await bridge.search_tools(
            "inspect", session_id=sid, server="first", refresh=True
        )
        assert len(launches) == 3
        await harness.control.session_coordinator.end_session(sid)
    finally:
        await harness.executor.aclose()
        await harness.control.aclose()
