"""External MCP integration management: one manifest, isolated owners, no inline secrets."""

import json
import os
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from tests.helpers import build_paired_mcp, mcp_structured
from workgate.agent_bridge.management import (
    ManagedMcpConfig,
    manage_mcp_manifest,
)
from workgate.agent_bridge.models import AgentSecretReference
from workgate.agent_bridge.state import load_agent_manifest
from workgate.config.settings import Settings


def _http_config(*, enabled: bool = True) -> ManagedMcpConfig:
    return ManagedMcpConfig(
        type="http", url="https://example.org/mcp", enabled=enabled
    )


def test_manifest_crud_is_atomic_validated_and_preserves_unrelated_configuration(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "agent"
    config_dir.mkdir()
    path = config_dir / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {"enabled": True, "directory": "managed"},
                "unrelated": {"setting": 42},
                "mcpServers": {
                    "legacy": {
                        "type": "http",
                        "url": "https://example.org/old?token=private-value",
                        "headers": {"Authorization": "private-secret"},
                        "integrationId": "legacy",
                        "extra_server_field": "preserve-me",
                    }
                },
            }
        )
    )
    manage_mcp_manifest(
        config_dir,
        "register",
        name="docs",
        config=_http_config(),
        owner_type="network",
    )
    assert (
        load_agent_manifest(config_dir).data.mcp_servers["docs"].type == "http"
    )
    result = manage_mcp_manifest(config_dir, "list", owner_type="network")
    assert {row["name"] for row in result["servers"]} == {"docs", "legacy"}
    assert "private-secret" not in json.dumps(result)
    assert "private-value" not in json.dumps(result)
    assert (
        manage_mcp_manifest(
            config_dir, "get", name="legacy", owner_type="network"
        )["server"]["headers"]["Authorization"]
        == "<redacted>"
    )

    manage_mcp_manifest(
        config_dir, "disable", name="legacy", owner_type="network"
    )
    manage_mcp_manifest(
        config_dir, "enable", name="legacy", owner_type="network"
    )
    raw = json.loads(path.read_text())
    assert raw["unrelated"]["setting"] == 42
    assert raw["mcpServers"]["legacy"]["extra_server_field"] == "preserve-me"
    assert (
        raw["mcpServers"]["legacy"]["headers"]["Authorization"]
        == "private-secret"
    )
    assert raw["mcpServers"]["legacy"]["enabled"] is True

    with pytest.raises(ValueError, match="already exists"):
        manage_mcp_manifest(
            config_dir,
            "register",
            name="docs",
            config=_http_config(),
            owner_type="network",
        )
    with pytest.raises(ValueError, match="owning plane"):
        manage_mcp_manifest(
            config_dir,
            "register",
            name="bad",
            config=ManagedMcpConfig(type="stdio", command="cat"),
            owner_type="network",
        )
    with pytest.raises(ValueError, match="Unknown"):
        manage_mcp_manifest(
            config_dir, "disable", name="missing", owner_type="network"
        )

    manage_mcp_manifest(
        config_dir,
        "update",
        name="docs",
        config=ManagedMcpConfig(
            type="http", url="https://example.org/new", enabled=False
        ),
        owner_type="network",
    )
    assert (
        load_agent_manifest(config_dir).data.mcp_servers["docs"].url
        == "https://example.org/new"
    )
    manage_mcp_manifest(config_dir, "remove", name="docs", owner_type="network")
    assert "docs" not in load_agent_manifest(config_dir).data.mcp_servers
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_management_secret_boundary_and_identity_safety(tmp_path: Path) -> None:
    for config in (
        {"type": "http", "url": "https://example.org/mcp?api_key=raw"},
        {"type": "http", "url": "https://user:password@example.org/mcp"},
        {
            "type": "http",
            "url": "https://example.org/mcp",
            "headers": {"Authorization": "raw-token"},
        },
        {"type": "stdio", "command": "git", "env": {"TOKEN": "raw-token"}},
    ):
        with pytest.raises(ValidationError):
            ManagedMcpConfig.model_validate(config)
    secret = ManagedMcpConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.org/mcp",
            "integrationId": "stable",
            "headers": {"X-API-Key": {"secret": "api_key"}},
            "auth": {"mode": "secret"},
        }
    )
    root = tmp_path / "config"
    manage_mcp_manifest(
        root, "register", name="api", config=secret, owner_type="network"
    )
    configured = (
        load_agent_manifest(root).data.mcp_servers["api"].headers["X-API-Key"]
    )
    assert isinstance(configured, AgentSecretReference)
    assert configured.secret == "api_key"
    with pytest.raises(ValueError, match="integrationId must be preserved"):
        manage_mcp_manifest(
            root,
            "update",
            name="api",
            config=_http_config(),
            owner_type="network",
        )
    assert (
        load_agent_manifest(root).data.mcp_servers["api"].integration_id
        == "stable"
    )

    path = root / "config.json"
    path.write_text("{ invalid json", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="manifest is invalid"):
        manage_mcp_manifest(root, "disable", name="api", owner_type="network")
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_public_control_executor_management_round_trip(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=True,
    )
    mcp, harness = build_paired_mcp(settings)
    try:
        names = {tool.name for tool in await mcp.list_tools()}
        assert "manage_agent_mcp_server" in names
        with pytest.raises(ToolError) as invalid:
            await mcp.call_tool(
                "manage_agent_mcp_server",
                {
                    "action": "register",
                    "name": "invalid",
                    "config": {
                        "type": "http",
                        "url": "https://example.org/mcp",
                        "integrationId": "invalid",
                        "auth": {"mode": "secret"},
                        "headers": {"Authorization": "DUMMY_SECRET_NEVER_USE"},
                    },
                },
            )
        assert "DUMMY_SECRET_NEVER_USE" not in str(invalid.value)
        with pytest.raises(ToolError, match="Invalid MCP config"):
            await mcp.call_tool(
                "manage_agent_mcp_server",
                {
                    "action": "register",
                    "name": "legacy-events",
                    "config": {
                        "type": "sse",
                        "url": "https://example.org/events",
                    },
                },
            )
        assert mcp_structured(
            await mcp.call_tool("manage_agent_mcp_server", {"action": "list"})
        ) == {"servers": []}
        response = mcp_structured(
            await mcp.call_tool(
                "manage_agent_mcp_server",
                {
                    "action": "register",
                    "name": "remote",
                    "config": {
                        "type": "http",
                        "url": "https://example.org/mcp",
                        "enabled": False,
                    },
                },
            )
        )
        assert response["updated"] is True
        control_only = mcp_structured(
            await mcp.call_tool("manage_agent_mcp_server", {"action": "list"})
        )
        assert [row["name"] for row in control_only["servers"]] == ["remote"]
        created = mcp_structured(
            await mcp.call_tool("session_start", {"workdir": str(workspace)})
        )
        sid = created["session_id"]
        with pytest.raises(ValueError) as remote_invalid:
            await harness.control.session_coordinator.call_session_tool(
                "agent_mcp.manage",
                {
                    "session_id": sid,
                    "action": "register",
                    "name": "invalid-local",
                    "config": {
                        "type": "stdio",
                        "command": sys.executable,
                        "env": {"TOKEN": "DUMMY_SECRET_NEVER_USE"},
                    },
                },
            )
        assert "DUMMY_SECRET_NEVER_USE" not in str(remote_invalid.value)
        registered = mcp_structured(
            await mcp.call_tool(
                "manage_agent_mcp_server",
                {
                    "action": "register",
                    "session_id": sid,
                    "name": "local",
                    "config": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": ["-c", "print('ok')"],
                        "enabled": False,
                    },
                },
            )
        )
        assert registered["updated"] is True
        assert (
            load_agent_manifest(harness.executor.config.agent_config_dir)
            .data.mcp_servers["local"]
            .type
            == "stdio"
        )
        combined = mcp_structured(
            await mcp.call_tool(
                "manage_agent_mcp_server", {"action": "list", "session_id": sid}
            )
        )
        assert {row["name"] for row in combined["servers"]} == {
            "local",
            "remote",
        }
        await mcp.call_tool(
            "manage_agent_mcp_server",
            {"action": "enable", "name": "local", "session_id": sid},
        )
        assert (
            load_agent_manifest(harness.executor.config.agent_config_dir)
            .data.mcp_servers["local"]
            .enabled
            is True
        )
        await mcp.call_tool(
            "manage_agent_mcp_server",
            {"action": "disable", "name": "local", "session_id": sid},
        )
        await mcp.call_tool(
            "manage_agent_mcp_server",
            {"action": "remove", "name": "local", "session_id": sid},
        )
        assert (
            "local"
            not in load_agent_manifest(
                harness.executor.config.agent_config_dir
            ).data.mcp_servers
        )
        await mcp.call_tool(
            "manage_agent_mcp_server", {"action": "remove", "name": "remote"}
        )
        await mcp.call_tool("session_end", {"session_id": sid})
    finally:
        await harness.executor.aclose()
        await harness.control.aclose()


@pytest.mark.asyncio
async def test_refresh_reprobes_executor_and_updates_discovery_cache(
    tmp_path: Path,
) -> None:
    from tests.helpers import build_paired_control_harness
    from workgate.agent_bridge.management import ManagedMcpConfig
    from workgate.config.control import resolve_control_config
    from workgate.control.agent_bridge import ControlAgentBridgeService

    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=True,
        agent_mcp_probe_timeout_s=15,
        agent_mcp_call_timeout_s=20,
    )
    harness = build_paired_control_harness(settings)
    bridge = ControlAgentBridgeService(
        resolve_control_config(settings), harness.control.session_coordinator
    )
    script = tmp_path / "server.py"

    def write_server(tool: str) -> None:
        script.write_text(
            "from mcp.server.mcpserver import MCPServer\n"
            "mcp = MCPServer('refresh-example')\n"
            f"@mcp.tool()\ndef {tool}() -> str:\n    return 'ok'\n"
            "mcp.run(transport='stdio')\n",
            encoding="utf-8",
        )

    try:
        created = await harness.control.session_coordinator.start_session(
            workdir=str(workspace), executor_id=harness.executor_id
        )
        assert isinstance(created, dict)
        sid = str(created["session_id"])
        write_server("before")
        await bridge.manage(
            "register",
            name="dynamic",
            session_id=sid,
            config=ManagedMcpConfig(
                type="stdio", command=sys.executable, args=[str(script)]
            ),
        )
        old = await bridge.search_tools("", session_id=sid, server="dynamic")
        assert [row["tool"] for row in old["tools"]] == ["before"]
        write_server("after")
        still_cached = await bridge.search_tools(
            "", session_id=sid, server="dynamic"
        )
        assert [row["tool"] for row in still_cached["tools"]] == ["before"]
        refreshed = await bridge.manage(
            "refresh", name="dynamic", session_id=sid
        )
        assert [row["tool"] for row in refreshed["tools"]] == ["after"]
        assert (await bridge.inspect_tool("dynamic", "after", session_id=sid))[
            "tool"
        ] == "after"
        await harness.control.session_coordinator.end_session(sid)
    finally:
        await harness.executor.aclose()
        await harness.control.aclose()


def test_cross_thread_registration_preserves_every_entry(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    root = tmp_path / "config"

    def add(index: int) -> None:
        manage_mcp_manifest(
            root,
            "register",
            name=f"service-{index}",
            config=_http_config(),
            owner_type="network",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(12)))
    assert len(load_agent_manifest(root).data.mcp_servers) == 12


def test_runtime_management_cannot_retarget_or_reuse_private_credentials(
    tmp_path: Path,
) -> None:
    from workgate.agent_bridge.auth_store import AgentAuthStore

    root, auth_dir = tmp_path / "agent", tmp_path / "auth"
    store = AgentAuthStore(auth_dir)
    config = ManagedMcpConfig.model_validate(
        {
            "type": "http",
            "url": "https://trusted.example/mcp",
            "integrationId": "stable",
            "headers": {"Authorization": {"secret": "token"}},
            "auth": {"mode": "secret"},
        }
    )
    manage_mcp_manifest(
        root,
        "register",
        name="trusted",
        config=config,
        owner_type="network",
        auth_dir=auth_dir,
    )
    store.set_secret("stable", "token", "private-credential")
    evil = config.model_copy(update={"url": "https://evil.example/mcp"})
    with pytest.raises(ValueError, match="retargeting"):
        manage_mcp_manifest(
            root,
            "update",
            name="trusted",
            config=evil,
            owner_type="network",
            auth_dir=auth_dir,
        )
    assert (
        load_agent_manifest(root).data.mcp_servers["trusted"].url
        == "https://trusted.example/mcp"
    )
    manage_mcp_manifest(
        root, "disable", name="trusted", owner_type="network", auth_dir=auth_dir
    )
    manage_mcp_manifest(
        root, "remove", name="trusted", owner_type="network", auth_dir=auth_dir
    )
    with pytest.raises(
        ValueError, match="reusing existing private credentials"
    ):
        manage_mcp_manifest(
            root,
            "register",
            name="renamed",
            config=evil,
            owner_type="network",
            auth_dir=auth_dir,
        )
    assert load_agent_manifest(root).data.mcp_servers == {}


def test_outbound_sse_is_rejected_by_runtime_management(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ManagedMcpConfig.model_validate(
            {"type": "sse", "url": "https://example.test/events"}
        )
    root = tmp_path / "config"
    root.mkdir()
    original = {
        "version": 1,
        "mcpServers": {
            "legacy": {"type": "sse", "url": "https://example.test/events"}
        },
    }
    (root / "config.json").write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is invalid"):
        manage_mcp_manifest(root, "list", owner_type="network")
    with pytest.raises(ValueError, match="manifest is invalid"):
        manage_mcp_manifest(
            root,
            "register",
            name="modern",
            config=ManagedMcpConfig(
                type="http", url="https://example.test/mcp"
            ),
            owner_type="network",
        )
    assert (
        json.loads((root / "config.json").read_text(encoding="utf-8"))
        == original
    )
