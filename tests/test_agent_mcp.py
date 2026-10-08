import asyncio
import json
import os
import queue
import signal
import sys
import threading
import time
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import Tool

import workgate.agent_bridge.mcp as agent_mcp_module
import workgate.agent_bridge.service as agent_service
from workgate.agent_bridge.mcp import (
    AgentMcpClientManager,
    AgentMcpTool,
    normalize_mcp_tool,
    normalize_tool_result,
)
from workgate.agent_bridge.models import AgentMcpServerConfig
from workgate.agent_bridge.registry import build_agent_registry
from workgate.agent_bridge.status import registry_config_status
from workgate.config.control import resolve_control_config
from workgate.config.settings import Settings


def test_normalize_mcp_tool_preserves_schema():
    sdk_tool = SimpleNamespace(
        name="search",
        description="Search docs",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    )

    assert normalize_mcp_tool(sdk_tool) == AgentMcpTool(
        name="search",
        description="Search docs",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    )


def test_normalize_mcp_tool_handles_sdk_tool_model():
    sdk_tool = Tool(
        name="search",
        description="Search docs",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    )

    assert normalize_mcp_tool(sdk_tool) == AgentMcpTool(
        name="search",
        description="Search docs",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    )


def test_normalize_tool_result_handles_text_content():
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello")],
        structuredContent=None,
        isError=False,
    )

    assert normalize_tool_result(result) == {
        "is_error": False,
        "content": [{"type": "text", "text": "hello"}],
        "structured_content": None,
    }


@pytest.mark.asyncio
async def test_client_manager_rejects_missing_stdio_command():
    manager = AgentMcpClientManager(call_timeout_s=1)
    server = AgentMcpServerConfig(type="stdio")

    with pytest.raises(ValueError, match="stdio MCP server requires command"):
        await manager.list_tools("broken", server)


def test_stdio_connections_end_after_each_discovery_or_call(monkeypatch):
    events: list[Any] = []

    @asynccontextmanager
    async def fake_stdio_client(params):
        events.append(("enter", params.command, params.cwd))
        try:
            yield object(), object()
        finally:
            events.append("exit")

    class FakeClientSession:
        def __init__(self, *_streams):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            events.append("session_exit")

        async def initialize(self):
            events.append("initialize")

        async def list_tools(self, *, params=None):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="ping",
                        description="Ping",
                        inputSchema={"type": "object"},
                    )
                ]
            )

        async def call_tool(self, tool, args):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="done")],
                structuredContent=None,
                isError=False,
            )

    monkeypatch.setattr(agent_mcp_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(agent_mcp_module, "ClientSession", FakeClientSession)
    manager = AgentMcpClientManager(
        call_timeout_s=2, stdio_cwd="/example/workdir"
    )
    server = AgentMcpServerConfig(type="stdio", command="fake-mcp")

    assert asyncio.run(manager.list_tools("local", server)) == [
        AgentMcpTool("ping", "Ping", {"type": "object"})
    ]
    assert asyncio.run(manager.call_tool("local", server, "ping"))[
        "content"
    ] == [{"type": "text", "text": "done"}]
    assert events == [
        ("enter", "fake-mcp", "/example/workdir"),
        "initialize",
        "session_exit",
        "exit",
        ("enter", "fake-mcp", "/example/workdir"),
        "initialize",
        "session_exit",
        "exit",
    ]


@pytest.mark.asyncio
async def test_control_stdio_is_forbidden():
    manager = AgentMcpClientManager(call_timeout_s=1, allow_stdio=False)
    server = AgentMcpServerConfig(type="stdio", command="fake-mcp")
    with pytest.raises(ValueError, match="executor-owned"):
        await manager.list_tools("local", server)
    with pytest.raises(ValueError, match="executor-owned"):
        await manager.call_tool("local", server, "ping")


@pytest.mark.asyncio
async def test_short_lived_stdio_child_reaped_on_initialize_timeout(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX process signal test")
    script = tmp_path / "hang.py"
    pid_file = tmp_path / "pid"
    script.write_text(
        """import json, os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
for line in sys.stdin:
    if json.loads(line).get('method') == 'initialize':
        while True:
            time.sleep(1)
""",
        encoding="utf-8",
    )
    manager = AgentMcpClientManager(call_timeout_s=0.5)
    server = AgentMcpServerConfig(
        type="stdio", command=sys.executable, args=[str(script), str(pid_file)]
    )
    try:
        with pytest.raises(TimeoutError):
            await manager.list_tools("hang", server)
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


def test_network_registry_uses_short_lived_manager_per_operation(tmp_path):
    settings = Settings(
        default_workdir=tmp_path / "workspace", state_dir=tmp_path / "state"
    )
    first = agent_service.build_network_agent_registry_from_settings(
        resolve_control_config(settings)
    )
    second = agent_service.build_network_agent_registry_from_settings(
        resolve_control_config(settings)
    )
    assert first.client_manager is not second.client_manager
    assert first.client_manager.allow_stdio is False
    assert second.client_manager.allow_stdio is False


@pytest.mark.asyncio
async def test_client_manager_rejects_missing_http_url():
    manager = AgentMcpClientManager(call_timeout_s=1)
    server = AgentMcpServerConfig(type="http")

    with pytest.raises(ValueError, match="http MCP server requires url"):
        await manager.list_tools("broken", server)


@pytest.mark.asyncio
async def test_client_manager_list_tools_accumulates_paginated_pages(
    monkeypatch,
):
    class FakeSession:
        def __init__(self):
            self.cursors = []

        async def list_tools(self, *, params=None):
            cursor = params.cursor if params else None
            self.cursors.append(cursor)
            if cursor is None:
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name="search",
                            description="Search docs",
                            inputSchema={"type": "object"},
                        )
                    ],
                    nextCursor="next-page",
                )
            if cursor == "next-page":
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name="fetch",
                            description="Fetch doc",
                            inputSchema={"type": "object"},
                        )
                    ]
                )
            raise AssertionError(f"unexpected cursor: {cursor}")

    manager = AgentMcpClientManager(call_timeout_s=1)
    server = AgentMcpServerConfig(type="http", url="https://example.test/mcp")
    session = FakeSession()

    @asynccontextmanager
    async def fake_session(name, session_server):
        assert name == "docs"
        assert session_server is server
        yield session

    monkeypatch.setattr(manager, "_session", fake_session)

    assert await manager.list_tools("docs", server) == [
        AgentMcpTool("search", "Search docs", {"type": "object"}),
        AgentMcpTool("fetch", "Fetch doc", {"type": "object"}),
    ]
    assert session.cursors == [None, "next-page"]


@pytest.mark.asyncio
async def test_client_manager_call_tool_uses_session_and_normalizes_result(
    monkeypatch,
):
    class FakeSession:
        def __init__(self):
            self.calls = []

        async def call_tool(self, tool, args):
            self.calls.append((tool, args))
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="done")],
                structuredContent={"ok": True},
                isError=False,
            )

    manager = AgentMcpClientManager(call_timeout_s=1)
    server = AgentMcpServerConfig(type="http", url="https://example.test/mcp")
    session = FakeSession()

    @asynccontextmanager
    async def fake_session(name, session_server):
        assert name == "docs"
        assert session_server is server
        yield session

    monkeypatch.setattr(manager, "_session", fake_session)

    assert await manager.call_tool(
        "docs", server, "search", {"query": "mcp"}
    ) == {
        "is_error": False,
        "content": [{"type": "text", "text": "done"}],
        "structured_content": {"ok": True},
    }
    assert session.calls == [("search", {"query": "mcp"})]


@pytest.mark.asyncio
async def test_client_manager_list_tools_cleans_up_session_on_use_error(
    monkeypatch,
):
    events = []

    class FakeSession:
        async def list_tools(self, *, params=None):
            cursor = params.cursor if params else None
            events.append(("list_tools", cursor))
            raise RuntimeError("list failed")

    manager = AgentMcpClientManager(call_timeout_s=1)
    server = AgentMcpServerConfig(type="http", url="https://example.test/mcp")

    @asynccontextmanager
    async def fake_session(name, session_server):
        assert name == "docs"
        assert session_server is server
        events.append("enter")
        try:
            yield FakeSession()
        finally:
            events.append("exit")

    monkeypatch.setattr(manager, "_session", fake_session)

    with pytest.raises(RuntimeError, match="list failed"):
        await manager.list_tools("docs", server)

    assert events == ["enter", ("list_tools", None), "exit"]


class FakeMcpManager:
    def __init__(self, tools_by_server=None, errors_by_server=None):
        self.tools_by_server = tools_by_server or {}
        self.errors_by_server = errors_by_server or {}

    async def list_tools(self, name, server):
        if name in self.errors_by_server:
            raise self.errors_by_server[name]
        return self.tools_by_server.get(name, [])


def test_build_agent_registry_probes_enabled_servers(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://example.com/mcp"},
                    "off": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "enabled": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    manager = FakeMcpManager(
        tools_by_server={
            "docs": [
                AgentMcpTool(
                    name="search",
                    description="Search docs",
                    input_schema={"type": "object"},
                )
            ]
        }
    )

    registry = build_agent_registry(tmp_path, manager, probe_timeout_s=1)

    assert registry.manifest_status == "loaded"
    assert registry.mcp_servers["docs"].available is True
    assert registry.mcp_servers["docs"].tools[0].name == "search"
    assert registry.mcp_servers["off"].available is False
    assert registry.mcp_servers["off"].error == "disabled"


def test_build_agent_registry_records_probe_errors(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {"bad": {"type": "http", "url": "https://bad"}},
            }
        ),
        encoding="utf-8",
    )

    registry = build_agent_registry(
        tmp_path,
        FakeMcpManager(errors_by_server={"bad": RuntimeError("boom")}),
        probe_timeout_s=1,
    )

    assert registry.mcp_servers["bad"].available is False
    assert registry.mcp_servers["bad"].error == "RuntimeError: boom"


def test_registry_config_status_redacts_probe_errors(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {"bad": {"type": "http", "url": "https://bad"}},
            }
        ),
        encoding="utf-8",
    )
    manager = FakeMcpManager(
        errors_by_server={
            "bad": RuntimeError(
                "Authorization: Bearer secret --token secret https://example.com?token=secret"
            )
        }
    )

    status = registry_config_status(
        build_agent_registry(tmp_path, manager, probe_timeout_s=1)
    )
    serialized = json.dumps(status)

    assert "secret" not in serialized.lower()
    assert "<redacted>" in serialized


def test_registry_config_status_redacts_env_and_header_values(tmp_path):
    env_token = "ghp_1234567890abcdef1234567890abcdef123456"
    header_value = "Bearer supersecret"
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "off": {
                        "type": "http",
                        "url": "https://off.example/mcp",
                        "enabled": False,
                        "env": {"CUSTOM": env_token},
                        "headers": {"X-Auth": header_value},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    status = registry_config_status(
        build_agent_registry(tmp_path, FakeMcpManager(), probe_timeout_s=1)
    )
    server = status["mcp_servers"]["off"]
    serialized = json.dumps(status)

    assert env_token not in serialized
    assert header_value not in serialized
    assert server["env"] == {"CUSTOM": "<redacted>"}
    assert server["headers"] == {"X-Auth": "<redacted>"}


def test_registry_config_status_redacts_configured_values_in_probe_errors(
    tmp_path,
):
    env_value = "custom-secret"
    header_value = "super-secret"
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "bad": {
                        "type": "http",
                        "url": "https://bad.example/mcp",
                        "env": {"CUSTOM": env_value},
                        "headers": {"X-Auth": header_value},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manager = FakeMcpManager(
        errors_by_server={
            "bad": RuntimeError(
                f"env={{'CUSTOM': '{env_value}'}} headers={{'X-Auth': '{header_value}'}}"
            )
        }
    )

    status = registry_config_status(
        build_agent_registry(tmp_path, manager, probe_timeout_s=1)
    )
    serialized = json.dumps(status)

    assert env_value not in serialized
    assert header_value not in serialized
    assert "<redacted>" in status["mcp_servers"]["bad"]["error"]


@pytest.mark.asyncio
async def test_build_agent_registry_works_inside_running_loop(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {"docs": {"type": "http", "url": "https://docs"}},
            }
        ),
        encoding="utf-8",
    )
    manager = FakeMcpManager(
        tools_by_server={
            "docs": [AgentMcpTool("search", "Search docs", {"type": "object"})]
        }
    )

    registry = build_agent_registry(tmp_path, manager, probe_timeout_s=1)

    assert registry.mcp_servers["docs"].available is True
    assert registry.mcp_servers["docs"].tools[0].name == "search"


def test_build_agent_registry_dynamic_flag_overrides(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "dynamicTools": {"mcp": False, "skills": False},
            }
        ),
        encoding="utf-8",
    )

    manifest_registry = build_agent_registry(
        tmp_path, FakeMcpManager(), probe_timeout_s=1
    )
    override_registry = build_agent_registry(
        tmp_path,
        FakeMcpManager(),
        probe_timeout_s=1,
        dynamic_mcp_tools=True,
        dynamic_skill_tools=True,
    )

    assert manifest_registry.dynamic_mcp_tools is False
    assert manifest_registry.dynamic_skill_tools is False
    assert override_registry.dynamic_mcp_tools is True
    assert override_registry.dynamic_skill_tools is True

    default_dir = tmp_path / "defaults"
    default_dir.mkdir()
    (default_dir / "config.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )

    default_registry = build_agent_registry(
        default_dir, FakeMcpManager(), probe_timeout_s=1
    )

    assert default_registry.dynamic_mcp_tools is True
    assert default_registry.dynamic_skill_tools is True


def test_build_agent_registry_records_timeout_without_hanging(tmp_path):
    class StubbornMcpManager:
        async def list_tools(self, name, server):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(60)

    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {"slow": {"type": "http", "url": "https://slow"}},
            }
        ),
        encoding="utf-8",
    )
    result_queue: queue.Queue[Any] = queue.Queue(maxsize=1)

    def run_registry() -> None:
        try:
            result_queue.put(
                build_agent_registry(
                    tmp_path, StubbornMcpManager(), probe_timeout_s=0.05
                )
            )
        except BaseException as exc:
            result_queue.put(exc)

    started_at = time.monotonic()
    thread = threading.Thread(target=run_registry, daemon=True)
    thread.start()

    result: Any = None
    try:
        result = result_queue.get(timeout=1)
    except queue.Empty:
        pytest.fail(
            "build_agent_registry did not return within the probe timeout"
        )

    elapsed = time.monotonic() - started_at
    assert elapsed < 1
    assert not isinstance(result, BaseException)
    record = result.mcp_servers["slow"]
    assert record.available is False
    assert (
        "timeout" in record.error.lower() or "timed out" in record.error.lower()
    )
