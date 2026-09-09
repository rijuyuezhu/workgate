"""Control-side tool catalog composition without machine implementation imports."""

from ..config.settings import Settings
from ..tools.catalog import ToolCatalog, build_tool_catalog
from .downloads import ControlDownloadService
from .jobs import ControlJobService
from .session_copy import ControlSessionCopyService
from .sessions import ControlSessionCoordinator
from .tool_routing import ControlToolRouter, route_control_registry


def build_control_tool_catalog(
    settings: Settings,
    sessions: ControlSessionCoordinator,
    session_copy: ControlSessionCopyService,
    jobs: ControlJobService,
    downloads: ControlDownloadService,
) -> ToolCatalog:
    """Build public metadata locally while routing machine calls through executors."""
    catalog = build_tool_catalog(settings)
    router = ControlToolRouter(sessions, session_copy, jobs, downloads)
    return ToolCatalog(
        tuple(
            route_control_registry(registry, router)
            for registry in catalog.registries
        )
    )
