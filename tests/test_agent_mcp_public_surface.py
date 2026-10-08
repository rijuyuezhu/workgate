"""The fixed Agent Bridge entrypoints, without generated external tool aliases."""

import asyncio
import json
from typing import Any, cast

import pytest

from workgate.agent_bridge.registry import (
    _run_async_blocking,
    build_agent_registry,
)
from workgate.tools.registry import agent as public_agent


@pytest.mark.asyncio
async def test_session_bound_public_entrypoints_require_control_routing() -> (
    None
):
    session_id = "sess_test"
    with pytest.raises(RuntimeError, match="control executor routing"):
        await public_agent.list_agent_skills.func(session_id)
    with pytest.raises(RuntimeError, match="control executor routing"):
        await public_agent.activate_agent_skill.func("foo", session_id)
    with pytest.raises(RuntimeError, match="control executor routing"):
        await public_agent.read_agent_skill_file.func(
            "foo", "readme.md", session_id
        )
    with pytest.raises(RuntimeError, match="control routing"):
        await public_agent.list_agent_mcp_servers.func(session_id)
    with pytest.raises(RuntimeError, match="control routing"):
        await public_agent.search_agent_mcp_tools.func(session_id=session_id)
    with pytest.raises(RuntimeError, match="control routing"):
        await public_agent.inspect_agent_mcp_tool.func(
            "docs", "search", session_id
        )
    with pytest.raises(RuntimeError, match="control routing"):
        await public_agent.call_agent_mcp_tool.func(
            "docs", "search", session_id=session_id
        )


@pytest.mark.asyncio
async def test_standalone_fixed_search_inspect_and_call(monkeypatch) -> None:
    tools = [
        {
            "server": "docs",
            "tool": "search",
            "description": "Find entries",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }
    ]
    monkeypatch.setattr(
        public_agent, "_agent_registry", lambda: cast(Any, object())
    )
    monkeypatch.setattr(
        public_agent,
        "list_agent_mcp_tools_payload",
        lambda _registry: cast(Any, type("ToolRows", (), {"tools": tools})()),
    )
    monkeypatch.setattr(
        public_agent,
        "list_agent_mcp_servers_payload",
        lambda _registry: cast(
            Any, type("Servers", (), {"root": {"docs": {"available": True}}})()
        ),
    )
    monkeypatch.setattr(
        public_agent,
        "call_agent_mcp_tool_payload",
        async_return_result,
    )
    public_agent._LOCAL_DISCOVERY.invalidate()
    assert (await public_agent.list_agent_mcp_servers.func()).root["docs"][
        "available"
    ]
    search = await public_agent.search_agent_mcp_tools.func(
        "find", refresh=True
    )
    assert search.tools == [
        {"server": "docs", "tool": "search", "description": "Find entries"}
    ]
    assert "input_schema" not in search.model_dump_json()
    inspected = await public_agent.inspect_agent_mcp_tool.func("docs", "search")
    assert inspected.input_schema["properties"]["query"]["type"] == "string"
    result = await public_agent.call_agent_mcp_tool.func(
        "docs", "search", {"query": "a"}
    )
    assert result.model_dump()["structured_content"] == {"ok": True}
    public_agent._LOCAL_DISCOVERY.invalidate()


async def async_return_result(_registry, server, tool, args):
    assert (server, tool, args) == ("docs", "search", {"query": "a"})
    from workgate.schemas.result_models.agent import CallAgentMcpToolOutput

    return CallAgentMcpToolOutput.model_validate(
        {"structured_content": {"ok": True}}
    )


def test_registry_blocking_runner_and_default_manager(tmp_path) -> None:
    async def value():
        return "ready"

    assert _run_async_blocking(value()) == "ready"
    assert asyncio.run(_run_inside_loop()) == "ready"
    registry = build_agent_registry(tmp_path, scan_skills=False)
    assert registry.mcp_servers == {}


async def _run_inside_loop():
    async def value():
        return "ready"

    return _run_async_blocking(value())


def test_registry_skips_other_transports_and_fails_closed_if_cursor_invalid(
    tmp_path,
) -> None:
    config_dir = tmp_path / "agent"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.test/mcp",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    class CursorUnavailable:
        def redaction_cursor(self, *_args):
            raise ValueError("private history invalid")

        async def list_tools(self, *_args):
            raise AssertionError("must not probe without valid history")

    manager = CursorUnavailable()
    skipped = build_agent_registry(
        config_dir,
        manager,
        scan_skills=False,
        mcp_server_types=frozenset({"stdio"}),
    )
    assert not skipped.mcp_servers
    registry = build_agent_registry(config_dir, manager, scan_skills=False)
    assert not registry.mcp_servers["remote"].available
    assert (
        registry.mcp_servers["remote"].error
        == "credential redaction history unavailable"
    )
