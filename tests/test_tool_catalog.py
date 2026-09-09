import pytest

from workgate.config.settings import Settings
from workgate.tools.catalog import (
    BUILTIN_TOOL_REGISTRY_FACTORIES,
    ToolCatalog,
    build_tool_catalog,
)
from workgate.tools.contracts import ToolRegistry
from workgate.tools.declarative import DeclarativeToolRegistry

EXPECTED_BUILTIN_REGISTRIES = (
    "agent",
    "audit",
    "downloads",
    "files",
    "image",
    "jobs",
    "patch",
    "read",
    "search",
    "secret_scan",
    "session",
    "shell",
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
async def test_factory_override_can_bind_a_narrow_dependency_to_handler() -> (
    None
):
    dependency = object()

    class BoundSearchRegistry(ToolRegistry):
        name = "search"

        def __init__(self, settings: Settings | None) -> None:
            self.settings = settings

        def http_handlers(self):
            async def search_handler(args):
                return {
                    "dependency": dependency,
                    "query": args["query"],
                }

            return {"search": search_handler}

    settings = Settings()
    catalog = build_tool_catalog(
        settings,
        factory_overrides={"search": BoundSearchRegistry},
    )
    registry = next(
        registry for registry in catalog.registries if registry.name == "search"
    )

    assert isinstance(registry, BoundSearchRegistry)
    assert registry.settings is settings
    result = await catalog.local_handlers()["search"]({"query": "needle"})
    assert result == {"dependency": dependency, "query": "needle"}


def test_catalog_rejects_unknown_factory_override() -> None:
    with pytest.raises(ValueError, match="Unknown built-in tool registry"):
        build_tool_catalog(
            factory_overrides={"plugin": lambda _settings: ToolRegistry()}
        )


def test_catalog_rejects_duplicate_local_handler_names() -> None:
    async def handler(_args):
        return None

    class FirstRegistry(ToolRegistry):
        def http_handlers(self):
            return {"duplicate": handler}

    class SecondRegistry(ToolRegistry):
        def http_handlers(self):
            return {"duplicate": handler}

    catalog = ToolCatalog((FirstRegistry(), SecondRegistry()))

    with pytest.raises(
        ValueError, match="Duplicate local tool handler: duplicate"
    ):
        catalog.local_handlers()
