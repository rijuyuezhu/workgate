"""Explicit built-in tool catalog construction for one app composition."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..config.settings import Settings
from .contracts import HttpToolRoute, McpToolContext, ToolHandler, ToolRegistry
from .registry.agent import AgentBridgeToolRegistry
from .registry.audit import AuditToolRegistry
from .registry.downloads import DownloadToolRegistry
from .registry.files import FileToolRegistry
from .registry.image import ImageToolRegistry
from .registry.jobs import JobToolRegistry
from .registry.patch import PatchToolRegistry
from .registry.read import ReadToolRegistry
from .registry.search import SearchToolRegistry
from .registry.secret_scan import SecretScanToolRegistry
from .registry.session import SessionToolRegistry
from .registry.shell import ShellToolRegistry
from .registry.todo import TodoToolRegistry
from .registry.transfer import TransferToolRegistry
from .registry.version import VersionToolRegistry
from .registry.workspace_connector import WorkspaceConnectorToolRegistry

type ToolRegistryFactory = Callable[[Settings | None], ToolRegistry]


BUILTIN_TOOL_REGISTRY_FACTORIES: tuple[tuple[str, ToolRegistryFactory], ...] = (
    ("agent", AgentBridgeToolRegistry),
    ("audit", AuditToolRegistry),
    ("downloads", DownloadToolRegistry),
    ("files", FileToolRegistry),
    ("image", ImageToolRegistry),
    ("jobs", JobToolRegistry),
    ("patch", PatchToolRegistry),
    ("read", ReadToolRegistry),
    ("search", SearchToolRegistry),
    ("secret_scan", SecretScanToolRegistry),
    ("session", SessionToolRegistry),
    ("shell", ShellToolRegistry),
    ("todo", TodoToolRegistry),
    ("transfer", TransferToolRegistry),
    ("version", VersionToolRegistry),
    ("workspace_connector", WorkspaceConnectorToolRegistry),
)
"""Immutable built-in registry membership and default construction factories."""


@dataclass(frozen=True)
class ToolCatalog:
    """Fresh registry instances and derived tool surfaces for one composition."""

    registries: tuple[ToolRegistry, ...]
    """Fresh built-in registry instances owned by this catalog."""

    def http_routes(self) -> tuple[HttpToolRoute, ...]:
        """Return all enabled REST route metadata from this catalog."""
        return tuple(
            route
            for registry in self.registries
            for route in registry.http_routes()
        )

    def local_handlers(self) -> Mapping[str, ToolHandler]:
        """Return the enabled local invocation handlers with duplicate checks."""
        handlers: dict[str, ToolHandler] = {}
        for registry in self.registries:
            for tool_name, handler in registry.http_handlers().items():
                if tool_name in handlers:
                    raise ValueError(
                        f"Duplicate local tool handler: {tool_name}"
                    )
                handlers[tool_name] = handler
        return MappingProxyType(handlers)

    def register_mcp(self, mcp, context: McpToolContext) -> None:
        """Register this catalog's MCP surface on one FastMCP application."""
        for registry in self.registries:
            registry.register_mcp(mcp, context)


def build_tool_catalog(
    settings: Settings | None = None,
    *,
    factory_overrides: Mapping[str, ToolRegistryFactory] | None = None,
) -> ToolCatalog:
    """Construct one fresh built-in catalog, optionally replacing known factories."""
    overrides = dict(factory_overrides or {})
    known_names = {name for name, _factory in BUILTIN_TOOL_REGISTRY_FACTORIES}
    unknown_names = set(overrides) - known_names
    if unknown_names:
        unknown = ", ".join(sorted(unknown_names))
        raise ValueError(
            f"Unknown built-in tool registry factory override: {unknown}"
        )
    registries = tuple(
        overrides.get(name, factory)(settings)
        for name, factory in BUILTIN_TOOL_REGISTRY_FACTORIES
    )
    return ToolCatalog(registries)
