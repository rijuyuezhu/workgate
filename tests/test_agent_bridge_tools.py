import hashlib
import json
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.auth import OAuthToken

from tests.helpers import build_paired_mcp, mcp_structured, mcp_text
from workgate.agent_bridge.auth_store import AgentAuthStore
from workgate.agent_bridge.mcp import AgentMcpClientManager, AgentMcpTool
from workgate.agent_bridge.registry import build_agent_registry
from workgate.agent_bridge.service import list_agent_mcp_tools_payload
from workgate.agent_bridge.status import registry_config_status
from workgate.agent_bridge.tools import AgentBridgeToolReloader
from workgate.app_paths import app_paths
from workgate.audit import get_audit_entry, query_audit
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control import agent_bridge as control_agent_bridge_module
from workgate.control.agent_bridge import ControlAgentBridgeService
from workgate.control.mcp.app import build_mcp
from workgate.tools.registry import agent as tools_module


def _payload(response: Any) -> dict[str, Any]:
    if isinstance(response, tuple):
        return cast(dict[str, Any], response[1])
    return cast(dict[str, Any], json.loads(mcp_text(response)))


@pytest.fixture(autouse=True)
def _share_agent_mcp_manager_test_factory(monkeypatch):
    """Keep legacy tool-registry fakes visible at the new control-owned seam."""

    def factory(timeout_s):
        return tools_module.AgentMcpClientManager(timeout_s)

    monkeypatch.setattr(
        control_agent_bridge_module, "AgentMcpClientManager", factory
    )


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
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    mcp, _harness = build_paired_mcp(get_settings())
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )
    response = await mcp.call_tool(
        "activate_agent_skill",
        {"session_id": session["session_id"], "name": "debugging"},
    )
    payload = mcp_text(response)

    assert "Find root causes." in payload
    assert "skills/debugging/SKILL.md" in payload


@pytest.mark.asyncio
async def test_control_exposes_no_dynamic_skill_aliases(tmp_path, monkeypatch):
    config_dir = app_paths().agent_config_dir
    managed_skill = config_dir / "skills" / "managed"
    managed_skill.mkdir(parents=True)
    (managed_skill / "SKILL.md").write_text(
        "# Managed\n\nManaged skill.\n", encoding="utf-8"
    )
    (config_dir / "config.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    project_skill = workspace / ".agents" / "skills" / "project-local"
    project_skill.mkdir(parents=True)
    (project_skill / "SKILL.md").write_text(
        "# Project Local\n\nSession-local skill.\n", encoding="utf-8"
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    tool_names = {tool.name for tool in await build_mcp().list_tools()}

    assert "activate_skill__managed" not in tool_names
    assert "activate_skill__project_local" not in tool_names


@pytest.mark.asyncio
async def test_session_bound_stdio_mcp_runs_on_executor_with_executor_secret(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "stdio-started"
    server_script = tmp_path / "stdio_server.py"
    server_script.write_text(
        """import hashlib
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

Path(os.environ["START_MARKER"]).write_text("started", encoding="utf-8")
mcp = FastMCP("executor-secret-test")

@mcp.tool()
def secret_fingerprint() -> dict[str, str]:
    return {"value": hashlib.sha256(os.environ["TOKEN"].encode()).hexdigest()}

@mcp.tool()
def echo_secret() -> dict[str, str]:
    return {"value": os.environ["TOKEN"]}

if __name__ == "__main__":
    mcp.run()
""",
        encoding="utf-8",
    )
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "stdio": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(server_script)],
                        "env": {
                            "TOKEN": {"secret": "token"},
                            "START_MARKER": str(marker),
                        },
                        "auth": {"mode": "secret"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    settings = get_settings()
    mcp, harness = build_paired_mcp(settings)
    AgentAuthStore(settings.agent_auth_dir).set_secret(
        "stdio", "token", "control-secret"
    )
    AgentAuthStore(harness.executor.config.agent_auth_dir).set_secret(
        "stdio", "token", "executor-secret"
    )
    try:
        assert _payload(await mcp.call_tool("list_agent_mcp_servers", {})) == {}
        assert not marker.exists()

        session = mcp_structured(
            await mcp.call_tool("session_start", {"workdir": "."})
        )
        session_id = session["session_id"]
        servers = _payload(
            await mcp.call_tool(
                "list_agent_mcp_servers", {"session_id": session_id}
            )
        )
        assert servers["stdio"]["available"] is True
        assert marker.read_text(encoding="utf-8") == "started"

        tools = _payload(
            await mcp.call_tool(
                "list_agent_mcp_tools", {"session_id": session_id}
            )
        )["tools"]
        assert sorted((row["server"], row["tool"]) for row in tools) == [
            ("stdio", "echo_secret"),
            ("stdio", "secret_fingerprint"),
        ]

        fingerprint = _payload(
            await mcp.call_tool(
                "call_agent_mcp_tool",
                {
                    "session_id": session_id,
                    "server": "stdio",
                    "tool": "secret_fingerprint",
                    "args": {},
                },
            )
        )
        assert fingerprint["structured_content"] == {
            "value": hashlib.sha256(b"executor-secret").hexdigest()
        }
        assert fingerprint["structured_content"] != {
            "value": hashlib.sha256(b"control-secret").hexdigest()
        }

        echoed = _payload(
            await mcp.call_tool(
                "call_agent_mcp_tool",
                {
                    "session_id": session_id,
                    "server": "stdio",
                    "tool": "echo_secret",
                    "args": {},
                },
            )
        )
        assert echoed["structured_content"] == {"value": "<redacted>"}
        public_json = json.dumps(echoed)
        assert "executor-secret" not in public_json
        assert "control-secret" not in public_json
        audit_entries = query_audit(search="call_agent_mcp_tool")["entries"]
        assert audit_entries
        for entry in audit_entries:
            full_entry = get_audit_entry(
                entry["id"], include_full_payloads=True
            )
            retained = json.dumps(full_entry, sort_keys=True)
            assert "executor-secret" not in retained
            assert "control-secret" not in retained
    finally:
        await harness.executor.aclose()
        await harness.control.aclose()


@pytest.mark.asyncio
async def test_control_oauth_refresh_credentials_are_redacted_from_result_and_audit(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "oauth": {
                        "type": "http",
                        "url": "https://oauth.example/mcp",
                        "auth": {"mode": "oauth"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    settings = get_settings()
    auth_store = AgentAuthStore(settings.agent_auth_dir)
    auth_store.set_tokens(
        "oauth",
        OAuthToken.model_validate(
            {
                "access_token": "oauth-access-before",
                "refresh_token": "oauth-refresh-private",
                "token_type": "Bearer",
            }
        ),
    )

    async def fake_list_tools(self, name, server):
        assert name == "oauth"
        assert server.auth.mode == "oauth"
        return [
            AgentMcpTool(
                name="echo_auth", description="Echo auth", input_schema={}
            )
        ]

    async def fake_call_tool(self, name, server, tool, args):
        assert name == "oauth"
        assert tool == "echo_auth"
        assert args == {}
        assert self.auth_store is not None
        before = self.auth_store.get_tokens(name)
        assert before is not None
        assert before.access_token == "oauth-access-before"
        self.auth_store.set_tokens(
            name,
            OAuthToken.model_validate(
                {
                    "access_token": "oauth-access-after",
                    "token_type": "Bearer",
                }
            ),
        )
        return {
            "structured_content": {
                "access_before": "oauth-access-before",
                "access_after": "oauth-access-after",
                "refresh": "oauth-refresh-private",
            }
        }

    monkeypatch.setattr(
        control_agent_bridge_module,
        "AgentMcpClientManager",
        AgentMcpClientManager,
    )
    monkeypatch.setattr(AgentMcpClientManager, "list_tools", fake_list_tools)
    monkeypatch.setattr(AgentMcpClientManager, "call_tool", fake_call_tool)

    result = _payload(
        await build_mcp().call_tool(
            "call_agent_mcp_tool",
            {"server": "oauth", "tool": "echo_auth", "args": {}},
        )
    )
    assert result["structured_content"] == {
        "access_before": "<redacted>",
        "access_after": "<redacted>",
        "refresh": "<redacted>",
    }
    public_json = json.dumps(result, sort_keys=True)
    for secret in (
        "oauth-access-before",
        "oauth-access-after",
        "oauth-refresh-private",
    ):
        assert secret not in public_json

    audit_entries = query_audit(search="call_agent_mcp_tool")["entries"]
    assert audit_entries
    for entry in audit_entries:
        retained = json.dumps(
            get_audit_entry(entry["id"], include_full_payloads=True),
            sort_keys=True,
        )
        for secret in (
            "oauth-access-before",
            "oauth-access-after",
            "oauth-refresh-private",
        ):
            assert secret not in retained


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


def test_agent_mcp_probe_rotation_redacts_public_capability_metadata(
    tmp_path,
) -> None:
    old_token = "oauth-access-before-probe"
    new_token = "oauth-access-after-probe"
    raw_tool_name = f"echo-{old_token}"
    config_dir = tmp_path / "agent"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "oauth": {
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "auth": {"mode": "oauth"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class RotatingProbeManager:
        def __init__(self) -> None:
            self.token = old_token

        def redaction_maps(self, _name, _server):
            return ({"oauth_access_token": self.token}, {})

        async def list_tools(self, _name, _server):
            assert self.token == old_token
            self.token = new_token
            return [
                AgentMcpTool(
                    name=raw_tool_name,
                    description=f"saw {old_token}",
                    input_schema={"note": old_token},
                )
            ]

    manager = RotatingProbeManager()
    registry = build_agent_registry(
        config_dir,
        manager,
        dynamic_mcp_tools=True,
        dynamic_skill_tools=False,
        scan_skills=False,
    )
    record = registry.mcp_servers["oauth"]
    rows = list_agent_mcp_tools_payload(registry).tools
    public_payload = json.dumps(rows)
    status_payload = json.dumps(registry_config_status(registry))

    assert manager.token == new_token
    assert record.raw_tool_names == (raw_tool_name,)
    assert record.probe_redaction_values == (old_token, new_token)
    assert old_token not in repr(record)
    assert new_token not in repr(record)
    assert record.tools[0].name == "echo-<redacted>"
    assert record.tools[0].description == "saw <redacted>"
    assert record.tools[0].input_schema == {"note": "<redacted>"}
    assert rows[0]["tool"] == "echo-<redacted>"
    assert rows[0]["description"] == "saw <redacted>"
    assert rows[0]["input_schema"] == {"note": "<redacted>"}
    assert old_token not in public_payload
    assert new_token not in public_payload
    assert old_token not in status_payload
    assert new_token not in status_payload

    dynamic_name, dynamic_record = next(
        iter(registry.dynamic_mcp_tool_map.items())
    )
    assert dynamic_record.tool_name == raw_tool_name
    assert old_token not in dynamic_name
    assert new_token not in dynamic_name

    class CapturingMcp:
        def __init__(self) -> None:
            self.descriptions: dict[str, str] = {}

        def add_tool(self, _handler, *, name, description, **_kwargs) -> None:
            self.descriptions[name] = description

        def remove_tool(self, name) -> None:
            self.descriptions.pop(name, None)

    public_mcp = CapturingMcp()
    reloader = AgentBridgeToolReloader(
        public_mcp,
        registry,
        {},
        5,
        True,
        False,
    )
    reloader.register_dynamic_tools()
    dynamic_description = public_mcp.descriptions[dynamic_name]
    assert old_token not in dynamic_description
    assert new_token not in dynamic_description
    assert "<redacted>" in dynamic_description


def test_agent_mcp_probe_rotation_redacts_probe_error(tmp_path) -> None:
    old_token = "oauth-access-before-probe"
    new_token = "oauth-access-after-probe"
    config_dir = tmp_path / "agent"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "oauth": {
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "auth": {"mode": "oauth"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class FailingRotatingProbeManager:
        def __init__(self) -> None:
            self.token = old_token

        def redaction_maps(self, _name, _server):
            return ({"oauth_access_token": self.token}, {})

        async def list_tools(self, _name, _server):
            assert self.token == old_token
            self.token = new_token
            raise RuntimeError(f"upstream echoed {old_token}")

    manager = FailingRotatingProbeManager()
    registry = build_agent_registry(
        config_dir,
        manager,
        dynamic_mcp_tools=False,
        dynamic_skill_tools=False,
        scan_skills=False,
    )
    record = registry.mcp_servers["oauth"]
    status_payload = json.dumps(registry_config_status(registry))

    assert manager.token == new_token
    assert record.error == "RuntimeError: upstream echoed <redacted>"
    assert record.error is not None
    assert old_token not in record.error
    assert new_token not in record.error
    assert old_token not in status_payload
    assert new_token not in status_payload
    assert "<redacted>" in status_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_dynamic_mcp_tool_redacts_retired_probe_credentials(
    tmp_path, monkeypatch, fail
):
    old_token = "m9X4q7V2z8N5p3K1"
    new_token = "n2C8r6T4y1B7d5F3"
    raw_tool_name = f"echo-{old_token}"
    dynamic_name = "agent_mcp__oauth__echo__redacted"
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "oauth": {
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "auth": {"mode": "oauth"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class RetiredCredentialManager:
        def __init__(self) -> None:
            self.token = old_token

        def redaction_maps(self, _name, _server):
            return ({"oauth_access_token": self.token}, {})

        async def list_tools(self, _name, _server):
            assert self.token == old_token
            self.token = new_token
            return [
                AgentMcpTool(
                    name=raw_tool_name,
                    description=f"saw {old_token}",
                    input_schema={},
                )
            ]

        async def call_tool(self, _name, _server, tool, _args):
            assert tool == raw_tool_name
            if fail:
                raise RuntimeError(f"upstream failed for tool {tool}")
            return {
                "structured_content": {"called": tool, "opaque": old_token},
                "content": [],
                "is_error": False,
            }

    manager = RetiredCredentialManager()
    monkeypatch.setattr(
        tools_module, "AgentMcpClientManager", lambda _timeout: manager
    )
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    clear_settings_cache()

    mcp = build_mcp()
    assert dynamic_name in {tool.name for tool in await mcp.list_tools()}

    if fail:
        with pytest.raises(ToolError) as exc_info:
            await mcp.call_tool(dynamic_name, {"args": {}})
        public_payload = str(exc_info.value)
    else:
        response = await mcp.call_tool(dynamic_name, {"args": {}})
        public_payload = mcp_text(response)

    assert old_token not in public_payload
    assert new_token not in public_payload
    assert "<redacted>" in public_payload

    audit_entries = query_audit(search=dynamic_name)["entries"]
    assert audit_entries
    for entry in audit_entries:
        retained = json.dumps(
            get_audit_entry(entry["id"], include_full_payloads=True),
            sort_keys=True,
        )
        assert old_token not in retained
        assert new_token not in retained


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
async def test_dynamic_skill_alias_is_not_control_local(tmp_path, monkeypatch):
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

    assert "activate_skill__paper_writer" not in tools
    assert "activate_agent_skill" in tools


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
async def test_control_dynamic_registry_ignores_skill_filesystem_changes(
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
    assert "activate_skill__paper_writer" not in tool_names
    assert "activate_skill__debugging" not in tool_names

    debugging_dir = config_dir / "skills" / "debugging"
    debugging_dir.mkdir()
    (debugging_dir / "SKILL.md").write_text(
        "# Debugging\n\nFind root causes.\n", encoding="utf-8"
    )

    tool_names = {tool.name for tool in await mcp.list_tools()}
    assert "activate_skill__paper_writer" not in tool_names
    assert "activate_skill__debugging" not in tool_names

    (skill_dir / "SKILL.md").unlink()
    tool_names = {tool.name for tool in await mcp.list_tools()}
    assert "activate_skill__paper_writer" not in tool_names
    assert "activate_skill__debugging" not in tool_names


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


@pytest.mark.asyncio
async def test_control_agent_bridge_rejects_same_name_across_owner_planes():
    class Sessions:
        async def call_session_tool(self, op, args):
            assert op == "agent_mcp.list_servers"
            assert args == {"session_id": "sess_owner"}
            return {"same": {"available": True}}

    service = ControlAgentBridgeService(
        cast(Any, object()), cast(Any, Sessions())
    )
    service._network_registry = lambda: cast(
        Any, SimpleNamespace(mcp_servers={"same": object()})
    )

    with pytest.raises(ValueError, match="ambiguous across control"):
        await service.list_tools("same", "sess_owner")
    with pytest.raises(ValueError, match="ambiguous across control"):
        await service.call_tool("same", "ping", {}, "sess_owner")


@pytest.mark.asyncio
async def test_control_agent_bridge_routes_by_explicit_owner(monkeypatch):
    class Sessions:
        async def call_session_tool(self, op, args):
            if op == "agent_mcp.list_servers":
                return {"executor": {"available": True}}
            if op == "agent_mcp.list_tools":
                return {
                    "tools": [
                        {
                            "server": "executor",
                            "tool": "local",
                            "description": "executor tool",
                        }
                    ]
                }
            if op == "agent_mcp.call_tool":
                return {"owner": "executor", "tool": args["tool"]}
            raise AssertionError(op)

    registry = cast(Any, SimpleNamespace(mcp_servers={"control": object()}))
    service = ControlAgentBridgeService(
        cast(Any, object()), cast(Any, Sessions())
    )
    service._network_registry = lambda: registry
    monkeypatch.setattr(
        control_agent_bridge_module,
        "list_agent_mcp_tools_payload",
        lambda _registry, server=None: (
            control_agent_bridge_module.ListAgentMcpToolsOutput(
                tools=[
                    {
                        "server": "control",
                        "tool": "network",
                        "description": "control tool",
                    }
                ]
                if server in {None, "control"}
                else []
            )
        ),
    )

    assert (await service._resolve_server_owner("control", "sess"))[
        1
    ] == "control"
    assert (await service._resolve_server_owner("executor", "sess"))[
        1
    ] == "executor"
    assert (await service._resolve_server_owner("missing", "sess"))[
        1
    ] == "unknown"
    assert (await service.list_tools("control", "sess")).tools[0][
        "tool"
    ] == "network"
    assert (await service.list_tools("executor", "sess")).tools[0][
        "tool"
    ] == "local"
    called = await service.call_tool("executor", "local", {"x": 1}, "sess")
    assert called.model_dump()["owner"] == "executor"
    with pytest.raises(ValueError, match="Unknown agent MCP server"):
        await service.list_tools("missing", "sess")
    with pytest.raises(ValueError, match="Unknown agent MCP server"):
        await service.call_tool("missing", "tool", {}, "sess")


@pytest.mark.asyncio
async def test_control_agent_bridge_rejects_duplicate_rows_when_listing_all(
    monkeypatch,
):
    class Sessions:
        async def call_session_tool(self, op, args):
            assert op == "agent_mcp.list_tools"
            assert args == {"session_id": "sess", "server": None}
            return {"tools": [{"server": "same", "tool": "executor"}]}

    service = ControlAgentBridgeService(
        cast(Any, object()), cast(Any, Sessions())
    )
    service._network_registry = lambda: cast(
        Any, SimpleNamespace(mcp_servers={"same": object()})
    )
    monkeypatch.setattr(
        control_agent_bridge_module,
        "list_agent_mcp_tools_payload",
        lambda _registry, server=None: (
            control_agent_bridge_module.ListAgentMcpToolsOutput(
                tools=[{"server": "same", "tool": "control"}]
            )
        ),
    )

    with pytest.raises(ValueError, match="duplicates: same"):
        await service.list_tools(session_id="sess")

    with pytest.raises(ValueError, match="duplicates: same"):
        service._merge_server_rows({"same": {}}, {"same": {}})
