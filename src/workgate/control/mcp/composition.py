"""MCP server composition independent of HTTP route mounting."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from mcp.server.mcpserver import MCPServer

from ... import __version__
from ...config.control import ControlConfig
from ...persistence import FileStateStore
from ...tools.catalog import ToolCatalog
from ...tools.contracts import McpToolContext
from ..runtime import ControlRuntime
from .config_management import register_config_management
from .image_preview import register_image_preview
from .instructions import SERVER_INSTRUCTIONS
from .live_workspace import register_live_workspace
from .watchdogs import WorkgateMCPServer


def _control_state_store(
    runtime: ControlRuntime | None, settings: ControlConfig
) -> FileStateStore:
    return (
        runtime.state_store
        if runtime is not None
        else FileStateStore(lambda: settings.state_dir)
    )


def build_mcp(
    *,
    settings: ControlConfig | None = None,
    tool_catalog: ToolCatalog | None = None,
    runtime: ControlRuntime | None = None,
    own_runtime_lifespan: bool = False,
    audit_transport: str = "mcp",
) -> MCPServer:
    """Create the MCP server from a routed control runtime or explicit catalog."""
    if runtime is not None:
        settings = runtime.config
    if settings is None:
        raise ValueError("MCP composition requires explicit configuration")
    if tool_catalog is not None:
        catalog = tool_catalog
    elif runtime is not None:
        catalog = runtime.tool_catalog
    else:  # pragma: no cover - guarded above.
        raise RuntimeError(
            "control MCP requires a routed runtime or explicit catalog"
        )

    @asynccontextmanager
    async def runtime_lifespan(_mcp: MCPServer) -> AsyncGenerator[None]:
        if runtime is None:
            yield
            return
        async with runtime.lifespan():
            yield

    state_store = _control_state_store(runtime, settings)
    mcp = WorkgateMCPServer(
        name="workgate",
        config=settings,
        audit_transport=audit_transport,
        state_store=state_store,
        oauth_state=None
        if runtime is None
        else getattr(runtime, "oauth_state", None),
        managed_jobs_runtime=(
            None
            if runtime is None
            else getattr(runtime, "managed_jobs_runtime", None)
        ),
        agent_activity_observer=(
            None
            if runtime is None or getattr(runtime, "task_service", None) is None
            else runtime.task_service.observe_agent_activity
        ),
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=(
            runtime_lifespan
            if runtime is not None and own_runtime_lifespan
            else None
        ),
    )
    cast(Any, mcp)._workgate_runtime = runtime
    cast(Any, mcp)._workgate_runtime_lifespan_owned = own_runtime_lifespan
    context = McpToolContext(settings=settings)
    catalog.register_mcp(mcp, context)
    register_live_workspace(mcp, runtime)
    register_image_preview(mcp, runtime)
    register_config_management(mcp, runtime)
    return mcp
