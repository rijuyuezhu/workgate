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


def test_client_manager_reuses_stdio_process_across_caller_event_loops(
    monkeypatch,
):
    events: list[Any] = []

    @asynccontextmanager
    async def fake_stdio_client(params):
        events.append(("transport_enter", params.command, tuple(params.args)))
        try:
            yield object(), object()
        finally:
            events.append("transport_exit")

    class FakeClientSession:
        def __init__(self, read_stream, write_stream):
            events.append("session_constructed")

        async def __aenter__(self):
            events.append("session_enter")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            events.append("session_exit")

        async def initialize(self):
            events.append("initialize")

        async def list_tools(self, *, params=None):
            cursor = params.cursor if params else None
            events.append(("list_tools", cursor))
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search",
                        description="Search docs",
                        inputSchema={"type": "object"},
                    )
                ]
            )

        async def call_tool(self, tool, args):
            events.append(("call_tool", tool, args))
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="done")],
                structuredContent=None,
                isError=False,
            )

    monkeypatch.setattr(agent_mcp_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(agent_mcp_module, "ClientSession", FakeClientSession)

    manager = AgentMcpClientManager(call_timeout_s=1)
    server = AgentMcpServerConfig(
        type="stdio", command="fake-mcp", args=["--serve"]
    )

    assert asyncio.run(manager.list_tools("browser", server)) == [
        AgentMcpTool("search", "Search docs", {"type": "object"})
    ]
    assert asyncio.run(
        manager.call_tool("browser", server, "search", {"query": "mcp"})
    ) == {
        "is_error": False,
        "content": [{"type": "text", "text": "done"}],
        "structured_content": None,
    }

    assert events.count("session_constructed") == 1
    assert events.count("session_enter") == 1
    assert events.count("initialize") == 1
    assert [event for event in events if isinstance(event, tuple)][0] == (
        "transport_enter",
        "fake-mcp",
        ("--serve",),
    )
    assert ("list_tools", None) in events
    assert ("call_tool", "search", {"query": "mcp"}) in events
    assert "transport_exit" not in events

    manager.close()

    assert events.count("session_exit") == 1
    assert events.count("transport_exit") == 1


def test_discard_stdio_worker_blocks_same_name_replacement(monkeypatch):
    events: list[str] = []
    close_started = threading.Event()
    allow_close = threading.Event()
    replacement: queue.Queue[Any] = queue.Queue()

    class OldWorker:
        usable = False

        def close(self) -> None:
            events.append("old-close-start")
            close_started.set()
            assert allow_close.wait(2)
            events.append("old-close-end")

    class NewWorker:
        usable = True

        def __init__(self, name, config):
            self.name = name
            events.append(f"{name}-constructed")

        def start(self) -> None:
            events.append(f"{self.name}-started")

    manager = AgentMcpClientManager(call_timeout_s=1)
    worker: Any = OldWorker()
    config = agent_mcp_module._StdioWorkerConfig("fake-mcp", (), ())
    manager._stdio_workers["browser"] = (config, worker)
    server = AgentMcpServerConfig(type="stdio", command="fake-mcp")
    monkeypatch.setattr(agent_mcp_module, "_PersistentStdioWorker", NewWorker)

    discard = threading.Thread(
        target=manager._discard_stdio_worker, args=("browser", worker)
    )
    discard.start()
    assert close_started.wait(1)

    replace = threading.Thread(
        target=lambda: replacement.put(manager._stdio_worker("browser", server))
    )
    replace.start()
    time.sleep(0.05)
    assert "browser-constructed" not in events

    other = manager._stdio_worker(
        "other", AgentMcpServerConfig(type="stdio", command="other-mcp")
    )
    assert other.__class__ is NewWorker
    assert "other-started" in events

    allow_close.set()
    discard.join(1)
    replace.join(1)

    assert not discard.is_alive()
    assert not replace.is_alive()
    assert replacement.get_nowait().__class__ is NewWorker
    assert events.index("old-close-end") < events.index("browser-started")


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM/SIGKILL regression")
def test_close_waits_until_stubborn_stdio_child_is_reaped(tmp_path):
    script = tmp_path / "stubborn_mcp.py"
    pid_file = tmp_path / "pid"
    script.write_text(
        """import json
import os
from pathlib import Path
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))

for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {},
            "serverInfo": {"name": "stubborn", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": []}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)

while True:
    time.sleep(1)
"""
    )
    manager = AgentMcpClientManager(call_timeout_s=2)
    server = AgentMcpServerConfig(
        type="stdio",
        command=sys.executable,
        args=[str(script), str(pid_file)],
    )

    assert asyncio.run(manager.list_tools("stubborn", server)) == []
    pid = int(pid_file.read_text())
    os.kill(pid, 0)

    try:
        manager.close()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-reaping regression")
def test_initialize_timeout_cancels_startup_and_reaps_stdio_child(tmp_path):
    script = tmp_path / "hung_initialize_mcp.py"
    pid_file = tmp_path / "pid"
    received_file = tmp_path / "received_initialize"
    script.write_text(
        """import json
import os
from pathlib import Path
import sys
import time

Path(sys.argv[1]).write_text(str(os.getpid()))
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        Path(sys.argv[2]).write_text("received")
        while True:
            time.sleep(1)
"""
    )
    manager = AgentMcpClientManager(call_timeout_s=0.5)
    server = AgentMcpServerConfig(
        type="stdio",
        command=sys.executable,
        args=[str(script), str(pid_file), str(received_file)],
    )
    pid: int | None = None

    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(manager.list_tools("hung", server))
        elapsed = time.monotonic() - started

        assert received_file.read_text() == "received"
        pid = int(pid_file.read_text())
        assert elapsed < 5
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert "hung" not in manager._stdio_workers
        assert not any(
            thread.name == "agent-mcp-stdio-hung" and thread.is_alive()
            for thread in threading.enumerate()
        )
    finally:
        with suppress(Exception):
            manager.close()
        if pid is None and pid_file.exists():
            pid = int(pid_file.read_text())
        if pid is not None:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_network_registry_reuses_service_mcp_manager(tmp_path):
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
    )

    first = agent_service.build_network_agent_registry_from_settings(settings)
    second = agent_service.build_network_agent_registry_from_settings(settings)

    assert first.client_manager is second.client_manager
    agent_service._close_shared_agent_mcp_client_manager()


def test_shared_manager_generation_retires_before_new_manager_is_visible(
    monkeypatch, tmp_path
):
    agent_service._close_shared_agent_mcp_client_manager()
    events: list[str] = []
    close_started = threading.Event()
    allow_close = threading.Event()
    results: queue.Queue[Any] = queue.Queue()
    instance_count = 0

    class FakeManager:
        def __init__(self, *_args, **_kwargs):
            nonlocal instance_count
            instance_count += 1
            self.number = instance_count
            events.append(f"manager-{self.number}-constructed")

        def close(self):
            if self.number == 1:
                events.append("old-close-start")
                close_started.set()
                assert allow_close.wait(2)
                events.append("old-close-end")

    monkeypatch.setattr(agent_service, "AgentMcpClientManager", FakeManager)
    first_settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        agent_mcp_call_timeout_s=1,
    )
    second_settings = Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        agent_mcp_call_timeout_s=2,
    )

    first: Any = agent_service._shared_agent_mcp_client_manager(first_settings)
    assert first.number == 1

    replace = threading.Thread(
        target=lambda: results.put(
            agent_service._shared_agent_mcp_client_manager(second_settings)
        )
    )
    replace.start()
    assert close_started.wait(1)

    concurrent = threading.Thread(
        target=lambda: results.put(
            agent_service._shared_agent_mcp_client_manager(second_settings)
        )
    )
    concurrent.start()
    time.sleep(0.05)
    assert results.empty()
    assert "manager-2-constructed" not in events

    allow_close.set()
    replace.join(1)
    concurrent.join(1)

    assert not replace.is_alive()
    assert not concurrent.is_alive()
    returned = [results.get_nowait(), results.get_nowait()]
    assert returned[0] is returned[1]
    assert returned[0].number == 2
    assert events.index("old-close-end") < events.index("manager-2-constructed")
    agent_service._close_shared_agent_mcp_client_manager()


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
