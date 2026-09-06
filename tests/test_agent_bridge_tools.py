import json
from typing import Any, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tests.helpers import mcp_text
from workgate.agent_bridge.mcp import AgentMcpTool
from workgate.app_paths import app_paths
from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp
from workgate.tools.registry import agent as tools_module


def _payload(response: Any) -> dict[str, Any]:
    if isinstance(response, tuple):
        return cast(dict[str, Any], response[1])
    return cast(dict[str, Any], json.loads(mcp_text(response)))


REALISTIC_SECRET_ERROR = (
    'env={"GITHUB_TOKEN": "ghp_secret"} '
    '{ "X-API-Key": "super secret with spaces!" } '
    "AWS_SECRET_ACCESS_KEY=abc123\n"
    "Authorization: Basic abc123\n"
    "Cookie: session=abc123; refresh=def456\n"
    "standalone sk-1234567890abcdef1234567890abcdef AKIA1234567890ABCDEF\n"
    "password: multi word secret"
)
REALISTIC_SECRET_VALUES = [
    "ghp_secret",
    "super secret with spaces!",
    "abc123",
    "def456",
    "sk-1234567890abcdef1234567890abcdef",
    "AKIA1234567890ABCDEF",
    "multi word secret",
]
CONFIGURED_ENV_VALUE = "custom-secret"
CONFIGURED_HEADER_VALUE = "super-secret"
CONFIGURED_VALUE_ERROR = f"env={{'CUSTOM': '{CONFIGURED_ENV_VALUE}'}} headers={{'X-Auth': '{CONFIGURED_HEADER_VALUE}'}}"
SERIALIZED_ENV_VALUE = "line1\nline2"
SERIALIZED_HEADER_VALUE = 'token "quoted" \\ path'
SERIALIZED_ENV_VALUE_ESCAPED = json.dumps(SERIALIZED_ENV_VALUE)[1:-1]
SERIALIZED_HEADER_VALUE_ESCAPED = json.dumps(SERIALIZED_HEADER_VALUE)[1:-1]
SERIALIZED_CONFIGURED_VALUE_ERROR = (
    f"env exact={SERIALIZED_ENV_VALUE} escaped={SERIALIZED_ENV_VALUE_ESCAPED} "
    f"headers exact={SERIALIZED_HEADER_VALUE} escaped={SERIALIZED_HEADER_VALUE_ESCAPED}"
)


def _assert_realistic_secret_values_redacted(payload: str) -> None:
    for secret in REALISTIC_SECRET_VALUES:
        assert secret not in payload
    assert "<redacted>" in payload


def _assert_configured_values_redacted(payload: str) -> None:
    assert CONFIGURED_ENV_VALUE not in payload
    assert CONFIGURED_HEADER_VALUE not in payload
    assert "<redacted>" in payload


def _assert_serialized_configured_values_redacted(
    payload: str, message: str
) -> None:
    payload_secret_forms = [
        SERIALIZED_ENV_VALUE,
        SERIALIZED_ENV_VALUE_ESCAPED,
        json.dumps(SERIALIZED_ENV_VALUE_ESCAPED)[1:-1],
        SERIALIZED_HEADER_VALUE,
        SERIALIZED_HEADER_VALUE_ESCAPED,
        json.dumps(SERIALIZED_HEADER_VALUE_ESCAPED)[1:-1],
    ]
    message_secret_forms = [
        SERIALIZED_ENV_VALUE,
        SERIALIZED_ENV_VALUE_ESCAPED,
        SERIALIZED_HEADER_VALUE,
        SERIALIZED_HEADER_VALUE_ESCAPED,
    ]
    for secret in payload_secret_forms:
        assert secret not in payload
    for secret in message_secret_forms:
        assert secret not in message
    assert "<redacted>" in payload
    assert "<redacted>" in message


@pytest.mark.asyncio
async def test_fixed_bridge_tools_exist_with_missing_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".workgate"))
    clear_settings_cache()

    mcp = build_mcp()
    tools = {tool.name for tool in await mcp.list_tools()}

    assert "agent_config_status" in tools
    assert "list_agent_skills" in tools
    assert "activate_agent_skill" in tools
    assert "read_agent_skill_file" in tools
    assert "list_agent_mcp_servers" in tools
    assert "list_agent_mcp_tools" in tools
    assert "call_agent_mcp_tool" in tools


@pytest.mark.asyncio
async def test_agent_config_status_reports_missing_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".workgate"))
    clear_settings_cache()

    response = await build_mcp().call_tool("agent_config_status", {})
    payload = mcp_text(response)

    assert "missing_config" in payload


@pytest.mark.asyncio
async def test_agent_config_status_redacts_probe_error(tmp_path, monkeypatch):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "bad": {
                        "type": "http",
                        "url": "https://bad.example/mcp",
                        "env": {"CUSTOM": CONFIGURED_ENV_VALUE},
                        "headers": {"X-Auth": CONFIGURED_HEADER_VALUE},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeMcpClientManager:
        async def list_tools(self, name, server):
            raise RuntimeError(
                f"{REALISTIC_SECRET_ERROR} {CONFIGURED_VALUE_ERROR}"
            )

        async def call_tool(self, name, server, tool, args):
            raise AssertionError("unavailable server should not be called")

    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda _timeout: FakeMcpClientManager(),
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    response = await build_mcp().call_tool("agent_config_status", {})
    payload = mcp_text(response)

    _assert_realistic_secret_values_redacted(payload)
    _assert_configured_values_redacted(payload)


@pytest.mark.asyncio
async def test_agent_config_status_redacts_env_and_header_values(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    env_token = "ghp_1234567890abcdef1234567890abcdef123456"
    header_value = "Bearer supersecret"
    (config_dir / "config.json").write_text(
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
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    response = await build_mcp().call_tool("agent_config_status", {})
    payload = mcp_text(response)
    server = _payload(response)["mcp_servers"]["off"]

    assert env_token not in payload
    assert header_value not in payload
    assert server["env"] == {"CUSTOM": "<redacted>"}
    assert server["headers"] == {"X-Auth": "<redacted>"}


@pytest.mark.asyncio
async def test_agent_config_status_redacts_serialized_configured_values(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "bad": {
                        "type": "http",
                        "url": "https://bad.example/mcp",
                        "env": {"CUSTOM": SERIALIZED_ENV_VALUE},
                        "headers": {"X-Auth": SERIALIZED_HEADER_VALUE},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeMcpClientManager:
        async def list_tools(self, name, server):
            raise RuntimeError(SERIALIZED_CONFIGURED_VALUE_ERROR)

        async def call_tool(self, name, server, tool, args):
            raise AssertionError("unavailable server should not be called")

    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda _timeout: FakeMcpClientManager(),
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    response = await build_mcp().call_tool("agent_config_status", {})
    payload = mcp_text(response)
    message = _payload(response)["mcp_servers"]["bad"]["error"]

    _assert_serialized_configured_values_redacted(payload, message)


@pytest.mark.asyncio
async def test_activate_agent_skill_returns_skill_content(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    skill_dir = config_dir / "skills" / "debugging"
    skill_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    (skill_dir / "SKILL.md").write_text(
        "# Debugging\n\nFind root causes.\n", encoding="utf-8"
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    response = await build_mcp().call_tool(
        "activate_agent_skill", {"name": "debugging"}
    )
    payload = mcp_text(response)

    assert "Find root causes." in payload
    assert "skills/debugging/SKILL.md" in payload


@pytest.mark.asyncio
async def test_agent_mcp_fixed_tools_route_and_reject_unavailable_servers(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://docs.example/mcp"},
                    "bad": {"type": "http", "url": "https://bad.example/mcp"},
                    "off": {
                        "type": "http",
                        "url": "https://off.example/mcp",
                        "enabled": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeMcpClientManager:
        def __init__(self):
            self.list_calls = []
            self.call_calls = []

        async def list_tools(self, name, server):
            self.list_calls.append((name, server.url))
            if name == "bad":
                raise RuntimeError("probe failed")
            return [
                AgentMcpTool(
                    name="search",
                    description="Search docs",
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                )
            ]

        async def call_tool(self, name, server, tool, args):
            self.call_calls.append((name, server.url, tool, args))
            return {"server": name, "tool": tool, "args": args}

    fake_manager = FakeMcpClientManager()
    monkeypatch.setattr(
        tools_module, "AgentMcpClientManager", lambda _timeout: fake_manager
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    mcp = build_mcp()

    servers = _payload(await mcp.call_tool("list_agent_mcp_servers", {}))
    assert set(servers) == {"docs", "bad", "off"}
    assert servers["docs"]["available"] is True
    assert servers["bad"]["available"] is False
    assert servers["off"]["available"] is False

    tools = _payload(await mcp.call_tool("list_agent_mcp_tools", {}))["tools"]
    assert tools == [
        {
            "server": "docs",
            "tool": "search",
            "description": "Search docs",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            "dynamic_tool_name": "agent_mcp__docs__search",
        }
    ]

    result = _payload(
        await mcp.call_tool(
            "call_agent_mcp_tool",
            {"server": "docs", "tool": "search", "args": {"query": "mcp"}},
        )
    )
    assert result == {
        "server": "docs",
        "tool": "search",
        "args": {"query": "mcp"},
    }
    assert fake_manager.call_calls == [
        ("docs", "https://docs.example/mcp", "search", {"query": "mcp"})
    ]

    with pytest.raises(ToolError, match="MCP server off is disabled"):
        await mcp.call_tool(
            "call_agent_mcp_tool",
            {"server": "off", "tool": "search", "args": {}},
        )

    with pytest.raises(
        ToolError,
        match="MCP server bad is unavailable: RuntimeError: probe failed",
    ):
        await mcp.call_tool(
            "call_agent_mcp_tool",
            {"server": "bad", "tool": "search", "args": {}},
        )

    with pytest.raises(ToolError, match="Unknown agent MCP server: missing"):
        await mcp.call_tool(
            "call_agent_mcp_tool",
            {"server": "missing", "tool": "search", "args": {}},
        )
    assert fake_manager.call_calls == [
        ("docs", "https://docs.example/mcp", "search", {"query": "mcp"})
    ]


@pytest.mark.asyncio
async def test_call_agent_mcp_tool_redacts_unavailable_probe_error(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "bad": {
                        "type": "http",
                        "url": "https://bad.example/mcp",
                        "env": {"CUSTOM": CONFIGURED_ENV_VALUE},
                        "headers": {"X-Auth": CONFIGURED_HEADER_VALUE},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeMcpClientManager:
        async def list_tools(self, name, server):
            raise RuntimeError(
                "Authorization: Bearer super-secret --token super-secret "
                "https://example.com?token=super-secret "
                '{"api_key": "super-secret"} '
                "{'token': 'super-secret'} "
                '["--token", "super-secret"] '
                "['--token', 'super-secret'] "
                "https://user:super-secret@example.com/path "
                f"{CONFIGURED_VALUE_ERROR}"
            )

        async def call_tool(self, name, server, tool, args):
            raise AssertionError("unavailable server should not be called")

    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda _timeout: FakeMcpClientManager(),
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    with pytest.raises(ToolError) as exc_info:
        await build_mcp().call_tool(
            "call_agent_mcp_tool",
            {"server": "bad", "tool": "search", "args": {}},
        )
    payload = str(exc_info.value)

    assert "super-secret" not in payload
    assert "<redacted>" in payload
    _assert_configured_values_redacted(payload)


@pytest.mark.asyncio
async def test_call_agent_mcp_tool_redacts_call_error(tmp_path, monkeypatch):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://docs.example/mcp",
                        "env": {"CUSTOM": CONFIGURED_ENV_VALUE},
                        "headers": {"X-Auth": CONFIGURED_HEADER_VALUE},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeMcpClientManager:
        async def list_tools(self, name, server):
            return [
                AgentMcpTool(
                    name="search",
                    description="Search docs",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, name, server, tool, args):
            raise RuntimeError(
                f"{REALISTIC_SECRET_ERROR} {CONFIGURED_VALUE_ERROR}"
            )

    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda _timeout: FakeMcpClientManager(),
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    with pytest.raises(ToolError) as exc_info:
        await build_mcp().call_tool(
            "call_agent_mcp_tool",
            {"server": "docs", "tool": "search", "args": {}},
        )
    payload = str(exc_info.value)

    _assert_realistic_secret_values_redacted(payload)
    _assert_configured_values_redacted(payload)


@pytest.mark.asyncio
async def test_call_agent_mcp_tool_redacts_serialized_configured_values(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://docs.example/mcp",
                        "env": {"CUSTOM": SERIALIZED_ENV_VALUE},
                        "headers": {"X-Auth": SERIALIZED_HEADER_VALUE},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeMcpClientManager:
        async def list_tools(self, name, server):
            return [
                AgentMcpTool(
                    name="search", description="Search docs", input_schema={}
                )
            ]

        async def call_tool(self, name, server, tool, args):
            raise RuntimeError(SERIALIZED_CONFIGURED_VALUE_ERROR)

    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda _timeout: FakeMcpClientManager(),
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    with pytest.raises(ToolError) as exc_info:
        await build_mcp().call_tool(
            "call_agent_mcp_tool",
            {"server": "docs", "tool": "search", "args": {}},
        )
    payload = str(exc_info.value)

    _assert_serialized_configured_values_redacted(payload, payload)


@pytest.mark.asyncio
async def test_call_agent_mcp_tool_redacts_error_payload(tmp_path, monkeypatch):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://docs.example/mcp",
                        "env": {"CUSTOM": CONFIGURED_ENV_VALUE},
                        "headers": {"X-Auth": CONFIGURED_HEADER_VALUE},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    class ErrorPayloadMcpManager:
        async def list_tools(self, name, server):
            return [
                AgentMcpTool(
                    name="search", description="Search docs", input_schema={}
                )
            ]

        async def call_tool(self, name, server, tool, args):
            return {
                "is_error": True,
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{REALISTIC_SECRET_ERROR} content env={CONFIGURED_ENV_VALUE} "
                            f"header={CONFIGURED_HEADER_VALUE}"
                        ),
                    }
                ],
                "structured_content": {
                    "details": [
                        f"structured env={CONFIGURED_ENV_VALUE}",
                        {"header": CONFIGURED_HEADER_VALUE},
                    ],
                    "keyed": {
                        f"env-{CONFIGURED_ENV_VALUE}": "env key",
                        CONFIGURED_HEADER_VALUE: "header key",
                    },
                },
            }

    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda _timeout: ErrorPayloadMcpManager(),
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    response = await build_mcp().call_tool(
        "call_agent_mcp_tool", {"server": "docs", "tool": "search", "args": {}}
    )
    payload = mcp_text(response)
    data = _payload(response)

    assert data["is_error"] is True
    _assert_realistic_secret_values_redacted(payload)
    _assert_configured_values_redacted(payload)
    assert "<redacted>" in data["content"][0]["text"]
    assert "<redacted>" in json.dumps(data["structured_content"])
    assert "env-<redacted>" in data["structured_content"]["keyed"]
    assert "<redacted>" in data["structured_content"]["keyed"]


@pytest.mark.asyncio
async def test_agent_mcp_public_metadata_redacts_configured_values(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    high_confidence_token = "sk-1234567890abcdef1234567890abcdef"
    upstream_tool_name = f"search-{CONFIGURED_ENV_VALUE}-{CONFIGURED_HEADER_VALUE}-{high_confidence_token}"
    schema_secret_key = f"query_{CONFIGURED_ENV_VALUE}"
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://docs.example/mcp",
                        "env": {"CUSTOM": CONFIGURED_ENV_VALUE},
                        "headers": {"X-Auth": CONFIGURED_HEADER_VALUE},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class MetadataLeakMcpManager:
        def __init__(self):
            self.call_calls = []

        async def list_tools(self, name, server):
            return [
                AgentMcpTool(
                    name=upstream_tool_name,
                    description=(
                        f"Search env={CONFIGURED_ENV_VALUE} header={CONFIGURED_HEADER_VALUE} "
                        f"token={high_confidence_token}"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            schema_secret_key: {
                                "type": "string",
                                "description": f"Uses {CONFIGURED_HEADER_VALUE}",
                                CONFIGURED_HEADER_VALUE: f"default {CONFIGURED_ENV_VALUE}",
                            }
                        },
                        "required": [schema_secret_key],
                    },
                )
            ]

        async def call_tool(self, name, server, tool, args):
            self.call_calls.append((name, tool, args))
            return {"ok": True}

    fake_manager = MetadataLeakMcpManager()
    monkeypatch.setattr(
        tools_module, "AgentMcpClientManager", lambda _timeout: fake_manager
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    mcp = build_mcp()
    rows = _payload(await mcp.call_tool("list_agent_mcp_tools", {}))["tools"]
    rows_payload = json.dumps(rows)

    for secret in (
        CONFIGURED_ENV_VALUE,
        CONFIGURED_HEADER_VALUE,
        high_confidence_token,
    ):
        assert secret not in rows_payload
    assert "<redacted>" in rows_payload

    row = rows[0]
    assert row["tool"] == "search-<redacted>-<redacted>-<redacted>"
    assert row["input_schema"]["properties"]["query_<redacted>"][
        "<redacted>"
    ] == ("default <redacted>")
    dynamic_tool_name = row["dynamic_tool_name"]
    assert "redacted" in dynamic_tool_name
    for secret in (
        CONFIGURED_ENV_VALUE,
        CONFIGURED_HEADER_VALUE,
        high_confidence_token,
    ):
        assert secret not in dynamic_tool_name

    dynamic_tool = {tool.name: tool for tool in await mcp.list_tools()}[
        dynamic_tool_name
    ]
    dynamic_description = dynamic_tool.description or ""
    for secret in (
        CONFIGURED_ENV_VALUE,
        CONFIGURED_HEADER_VALUE,
        high_confidence_token,
    ):
        assert secret not in dynamic_description
    assert "<redacted>" in dynamic_description

    await mcp.call_tool(dynamic_tool_name, {"args": {"query": "abc"}})
    assert fake_manager.call_calls == [
        ("docs", upstream_tool_name, {"query": "abc"})
    ]


class FakeDynamicMcpManager:
    async def list_tools(self, name, server):
        if name == "docs":
            return [
                AgentMcpTool(
                    name="search",
                    description="Search docs",
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                )
            ]
        return []

    async def call_tool(self, name, server, tool, args):
        return {
            "server": name,
            "tool": tool,
            "args": args,
            "content": [{"type": "text", "text": "ok"}],
        }


@pytest.mark.asyncio
async def test_dynamic_skill_tool_is_visible_and_callable(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    skill_dir = config_dir / "skills" / "paper-writer"
    skill_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    (skill_dir / "SKILL.md").write_text(
        "# Paper Writer\n\nDraft papers.\n", encoding="utf-8"
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    mcp = build_mcp()
    tools = {tool.name for tool in await mcp.list_tools()}

    assert "activate_skill__paper_writer" in tools
    response = await mcp.call_tool("activate_skill__paper_writer", {})
    assert "Draft papers." in mcp_text(response)


@pytest.mark.asyncio
async def test_dynamic_mcp_tool_is_visible_and_callable(tmp_path, monkeypatch):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://example.com/mcp"}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda timeout: FakeDynamicMcpManager(),
    )
    clear_settings_cache()

    mcp = build_mcp()
    tool_names = {tool.name for tool in await mcp.list_tools()}

    assert "agent_mcp__docs__search" in tool_names
    response = await mcp.call_tool(
        "agent_mcp__docs__search", {"args": {"query": "abc"}}
    )
    assert "abc" in mcp_text(response)


@pytest.mark.asyncio
async def test_dynamic_mcp_tool_redacts_configured_values_in_call_error(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "env": {"CUSTOM": CONFIGURED_ENV_VALUE},
                        "headers": {"X-Auth": CONFIGURED_HEADER_VALUE},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class FailingDynamicMcpManager:
        async def list_tools(self, name, server):
            return [
                AgentMcpTool(
                    name="search",
                    description="Search docs",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, name, server, tool, args):
            raise RuntimeError(CONFIGURED_VALUE_ERROR)

    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda timeout: FailingDynamicMcpManager(),
    )
    clear_settings_cache()

    with pytest.raises(ToolError) as exc_info:
        await build_mcp().call_tool("agent_mcp__docs__search", {"args": {}})
    payload = str(exc_info.value)

    _assert_configured_values_redacted(payload)


@pytest.mark.asyncio
async def test_dynamic_mcp_tool_redacts_error_payload(tmp_path, monkeypatch):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "env": {"CUSTOM": CONFIGURED_ENV_VALUE},
                        "headers": {"X-Auth": CONFIGURED_HEADER_VALUE},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class ErrorPayloadDynamicMcpManager:
        async def list_tools(self, name, server):
            return [
                AgentMcpTool(
                    name="search", description="Search docs", input_schema={}
                )
            ]

        async def call_tool(self, name, server, tool, args):
            return {
                "is_error": True,
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{REALISTIC_SECRET_ERROR} content env={CONFIGURED_ENV_VALUE} "
                            f"header={CONFIGURED_HEADER_VALUE}"
                        ),
                    }
                ],
                "structured_content": {
                    "details": {
                        "env": CONFIGURED_ENV_VALUE,
                        "message": f"header={CONFIGURED_HEADER_VALUE}",
                    },
                    "keyed": {
                        f"env-{CONFIGURED_ENV_VALUE}": "env key",
                        CONFIGURED_HEADER_VALUE: "header key",
                    },
                },
            }

    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda _timeout: ErrorPayloadDynamicMcpManager(),
    )
    clear_settings_cache()

    response = await build_mcp().call_tool(
        "agent_mcp__docs__search", {"args": {}}
    )
    payload = mcp_text(response)
    data = _payload(response)

    assert data["is_error"] is True
    _assert_realistic_secret_values_redacted(payload)
    _assert_configured_values_redacted(payload)
    assert "<redacted>" in data["content"][0]["text"]
    assert "<redacted>" in json.dumps(data["structured_content"])
    assert "env-<redacted>" in data["structured_content"]["keyed"]
    assert "<redacted>" in data["structured_content"]["keyed"]


@pytest.mark.asyncio
async def test_build_mcp_respects_manifest_dynamic_tool_disable(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    skill_dir = config_dir / "skills" / "paper-writer"
    skill_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://example.com/mcp"}
                },
                "dynamicTools": {"mcp": False, "skills": False},
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "# Paper Writer\n\nDraft papers.\n", encoding="utf-8"
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda timeout: FakeDynamicMcpManager(),
    )
    clear_settings_cache()

    mcp = build_mcp()
    tool_names = {tool.name for tool in await mcp.list_tools()}
    status = _payload(await mcp.call_tool("agent_config_status", {}))

    assert "activate_skill__paper_writer" not in tool_names
    assert "agent_mcp__docs__search" not in tool_names
    assert status["dynamic_tools"] == {"mcp": False, "skills": False}


@pytest.mark.asyncio
async def test_agent_bridge_hot_reloads_dynamic_skill_tools(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    skill_dir = config_dir / "skills" / "paper-writer"
    skill_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    (skill_dir / "SKILL.md").write_text(
        "# Paper Writer\n\nDraft papers.\n", encoding="utf-8"
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    mcp = build_mcp()
    tool_names = {tool.name for tool in await mcp.list_tools()}
    assert "activate_skill__paper_writer" in tool_names
    assert "activate_skill__debugging" not in tool_names

    debugging_dir = config_dir / "skills" / "debugging"
    debugging_dir.mkdir()
    (debugging_dir / "SKILL.md").write_text(
        "# Debugging\n\nFind root causes.\n", encoding="utf-8"
    )

    tool_names = {tool.name for tool in await mcp.list_tools()}
    assert "activate_skill__paper_writer" in tool_names
    assert "activate_skill__debugging" in tool_names
    response = await mcp.call_tool("activate_skill__debugging", {})
    assert "Find root causes." in mcp_text(response)

    (skill_dir / "SKILL.md").unlink()
    tool_names = {tool.name for tool in await mcp.list_tools()}
    assert "activate_skill__paper_writer" not in tool_names
    assert "activate_skill__debugging" in tool_names


@pytest.mark.asyncio
async def test_agent_bridge_hot_reloads_mcp_server_tools(tmp_path, monkeypatch):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )

    class ReloadingMcpManager:
        def __init__(self):
            self.call_calls = []

        async def list_tools(self, name, server):
            return [
                AgentMcpTool(
                    name="search", description=f"Search {name}", input_schema={}
                )
            ]

        async def call_tool(self, name, server, tool, args):
            self.call_calls.append((name, server.url, tool, args))
            return {
                "server": name,
                "url": server.url,
                "tool": tool,
                "args": args,
            }

    fake_manager = ReloadingMcpManager()
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    monkeypatch.setattr(
        tools_module, "AgentMcpClientManager", lambda _timeout: fake_manager
    )
    clear_settings_cache()

    mcp = build_mcp()
    tool_names = {tool.name for tool in await mcp.list_tools()}
    assert "agent_mcp__docs__search" not in tool_names

    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://docs.example/mcp"}
                },
            }
        ),
        encoding="utf-8",
    )
    tool_names = {tool.name for tool in await mcp.list_tools()}
    assert "agent_mcp__docs__search" in tool_names
    response = await mcp.call_tool(
        "agent_mcp__docs__search", {"args": {"query": "abc"}}
    )
    assert _payload(response) == {
        "server": "docs",
        "url": "https://docs.example/mcp",
        "tool": "search",
        "args": {"query": "abc"},
    }

    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "api": {"type": "http", "url": "https://api.example/mcp"},
                },
            }
        ),
        encoding="utf-8",
    )
    tool_names = {tool.name for tool in await mcp.list_tools()}
    assert "agent_mcp__docs__search" not in tool_names
    assert "agent_mcp__api__search" in tool_names

    response = await mcp.call_tool(
        "call_agent_mcp_tool", {"server": "api", "tool": "search"}
    )
    assert _payload(response)["url"] == "https://api.example/mcp"
