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

EXPECTED_BUILTIN_REGISTRIES = (
    "agent",
    "audit",
    "browser",
    "downloads",
    "executor",
    "files",
    "gui",
    "image",
    "jobs",
    "patch",
    "read",
    "search",
    "secret_scan",
    "session",
    "shell",
    "task",
    "todo",
    "transfer",
    "version",
    "workspace_connector",
)


def test_builtin_registry_membership_is_explicit_and_stable() -> None:
    assert tuple(
        name for name, _factory in BUILTIN_TOOL_REGISTRY_FACTORIES
    ) == (EXPECTED_BUILTIN_REGISTRIES)


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
