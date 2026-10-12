import pytest
from mcp.types import CallToolResult

from workgate.config.control import ControlConfig, resolve_control_config
from workgate.config.settings import Settings
from workgate.control.mcp.app import build_mcp
from workgate.tools.catalog import (
    BUILTIN_TOOL_REGISTRY_FACTORIES,
    build_tool_catalog,
)
from workgate.tools.contracts import ToolRegistry
from workgate.tools.declarative import DeclarativeToolRegistry


def test_builtin_registry_names_are_unique() -> None:
    names = [name for name, _factory in BUILTIN_TOOL_REGISTRY_FACTORIES]
    assert names
    assert len(names) == len(set(names))


def test_catalog_builds_fresh_instances_but_shares_immutable_definitions() -> (
    None
):
    first = build_tool_catalog()
    second = build_tool_catalog()

    assert tuple(type(registry) for registry in first.registries) == tuple(
        factory for _name, factory in BUILTIN_TOOL_REGISTRY_FACTORIES
    )
    assert tuple(type(registry) for registry in second.registries) == tuple(
        factory for _name, factory in BUILTIN_TOOL_REGISTRY_FACTORIES
    )
    assert all(
        first_registry is not second_registry
        for first_registry, second_registry in zip(
            first.registries, second.registries, strict=True
        )
    )

    first_search = next(
        registry for registry in first.registries if registry.name == "search"
    )
    second_search = next(
        registry for registry in second.registries if registry.name == "search"
    )
    assert isinstance(first_search, DeclarativeToolRegistry)
    assert isinstance(second_search, DeclarativeToolRegistry)
    assert first_search.tools is second_search.tools


@pytest.mark.asyncio
async def test_factory_override_binds_dependency_to_canonical_mcp_tool() -> (
    None
):
    dependency = "bound dependency"

    class BoundSearchRegistry(ToolRegistry):
        name = "search"

        def __init__(self, settings: ControlConfig | None) -> None:
            self.settings = settings

        def register_mcp(self, mcp, context):
            @mcp.tool()
            async def search(query: str) -> dict[str, str]:
                return {"dependency": dependency, "query": query}

    settings = resolve_control_config(Settings())
    catalog = build_tool_catalog(
        settings,
        factory_overrides={"search": BoundSearchRegistry},
    )
    registry = next(
        registry for registry in catalog.registries if registry.name == "search"
    )
    assert isinstance(registry, BoundSearchRegistry)
    assert registry.settings is settings
    mcp = build_mcp(tool_catalog=catalog)
    output = await mcp.call_tool("search", {"query": "needle"})
    assert isinstance(output, CallToolResult)
    assert output.structured_content == {
        "dependency": dependency,
        "query": "needle",
    }


def test_catalog_rejects_unknown_factory_override() -> None:
    with pytest.raises(ValueError, match="Unknown built-in tool registry"):
        build_tool_catalog(
            factory_overrides={"plugin": lambda _settings: ToolRegistry()}
        )


@pytest.mark.asyncio
async def test_executor_transfer_operations_are_not_public_tools(
    tmp_path, monkeypatch
) -> None:
    """Internal executor operations stay callable only through its dispatcher."""
    from fastapi.testclient import TestClient

    from workgate.config.settings import clear_settings_cache
    from workgate.control.http.app import build_http_app
    from workgate.executor.dispatch import EXECUTOR_INTERNAL_OPERATION_NAMES

    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    internal_ops = {
        name
        for name in EXECUTOR_INTERNAL_OPERATION_NAMES
        if name.startswith("transfer_")
    }
    assert {"transfer_stat", "transfer_begin_write"} <= internal_ops

    mcp_names = {tool.name for tool in await build_mcp().list_tools()}
    assert internal_ops.isdisjoint(mcp_names)

    with TestClient(
        build_http_app(tool_catalog=build_tool_catalog())
    ) as client:
        public_names = {
            tool["name"] for tool in client.get("/tools").json()["tools"]
        }
        assert "read" in public_names
        assert internal_ops.isdisjoint(public_names)

        response = client.post("/tools/transfer_stat", json={"path": "."})
        assert response.status_code == 200
        assert response.json()["isError"] is True
        assert "Unknown tool" in response.json()["content"][0]["text"]


def test_base_tool_registry_registers_nothing() -> None:
    """An empty registry is legal without defining a second tool surface."""
    from mcp.server.mcpserver import MCPServer

    empty = ToolRegistry()
    mcp = MCPServer("empty-catalog")
    from workgate.tools.contracts import McpToolContext

    empty.register_mcp(
        mcp, McpToolContext(settings=resolve_control_config(Settings()))
    )
