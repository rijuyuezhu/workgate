"""Build and run the MCP server."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount

from ...audit import audit
from ...config.settings import Settings, get_settings
from ...http.public_routes import public_http_routes
from ...http.request_limits import install_request_body_limit
from ...oauth.core.security import validate_public_oauth_configuration
from ...oauth.http.middleware import AuthMiddleware
from ...oauth.http.routes import oauth_public_routes
from ...ops.shell import tool_timeout_s
from ...remote.http import remote_routes
from ...remote.transfer_gateway import build_transfer_gateway_router
from ...tools.catalog import ToolCatalog, build_tool_catalog
from ...tools.contracts import McpToolContext
from ...tools.metadata import install_tool_safety_annotations
from ...ui.http.routes import human_ui_routes
from ..http.executor_routes import executor_routes
from ..runtime import ControlRuntime, build_control_runtime
from .instructions import SERVER_INSTRUCTIONS
from .session_limits import McpSessionLimitMiddleware
from .transport_security import transport_security_settings
from .watchdogs import install_mcp_tool_watchdogs


def _make_read_only_tool_annotations() -> ToolAnnotations:
    """Mark a tool as read-only for MCP clients."""
    return ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def build_mcp(
    *,
    tool_catalog: ToolCatalog | None = None,
    runtime: ControlRuntime | None = None,
    own_runtime_lifespan: bool = False,
) -> FastMCP:
    """Create the MCP server and register one explicit local tool catalog."""
    settings = (
        runtime.legacy_settings if runtime is not None else get_settings()
    )
    catalog = tool_catalog or (
        runtime.tool_catalog
        if runtime is not None
        else build_tool_catalog(settings)
    )

    @asynccontextmanager
    async def runtime_lifespan(_mcp: FastMCP) -> AsyncGenerator[None]:
        if runtime is None:
            yield
            return
        async with runtime.lifespan():
            yield

    mcp = FastMCP(
        "workgate",
        instructions=SERVER_INSTRUCTIONS,
        transport_security=transport_security_settings(settings),
        lifespan=(
            runtime_lifespan
            if runtime is not None and own_runtime_lifespan
            else None
        ),
    )
    context = McpToolContext(
        settings=settings,
        read_only_tool_annotations=_make_read_only_tool_annotations(),
    )
    catalog.register_mcp(mcp, context)
    install_tool_safety_annotations(mcp)
    install_mcp_tool_watchdogs(mcp)
    return mcp


def _add_public_routes_to_mcp_http_app(
    mcp_app: Starlette,
    *,
    settings: Settings | None = None,
    runtime: ControlRuntime | None = None,
) -> tuple[Starlette, list[BaseRoute]]:
    """Serve health/OAuth routes directly and send everything else to MCP."""
    active_settings = (
        settings
        if settings is not None
        else runtime.legacy_settings
        if runtime is not None
        else get_settings()
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        if runtime is None:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
            return
        async with (
            runtime.lifespan(),
            mcp_app.router.lifespan_context(mcp_app),
        ):
            yield

    public_routes: list[BaseRoute] = [
        *public_http_routes(
            active_settings,
            readyz_include_workspace_root=False,
        ),
        *(
            executor_routes(runtime.executor_transport)
            if runtime is not None
            else ()
        ),
        *(remote_routes() if active_settings.remote_enabled else ()),
        *(
            build_transfer_gateway_router()
            if active_settings.remote_enabled
            and active_settings.remote_http_transfer_enabled
            else ()
        ),
        *oauth_public_routes(),
    ]
    ui_routes, ui_public_routes = human_ui_routes(active_settings)
    routes = [*public_routes, *ui_routes, Mount("/", app=mcp_app)]
    public_routes.extend(ui_public_routes)
    return Starlette(routes=routes, lifespan=lifespan), public_routes


def _build_authenticated_mcp_http_app(
    mcp_app: Starlette,
    *,
    session_manager: object | None = None,
    mcp_path: str = "/mcp",
    settings: Settings | None = None,
    runtime: ControlRuntime | None = None,
) -> Starlette:
    """Add resource limits and OAuth protection around the MCP HTTP app."""
    active_settings = (
        settings
        if settings is not None
        else runtime.legacy_settings
        if runtime is not None
        else get_settings()
    )
    app, public_routes = _add_public_routes_to_mcp_http_app(
        mcp_app,
        settings=active_settings,
        runtime=runtime,
    )
    if session_manager is not None and not bool(
        getattr(session_manager, "stateless", False)
    ):
        app.add_middleware(
            McpSessionLimitMiddleware,
            session_manager=session_manager,
            max_sessions=active_settings.mcp_max_sessions,
            mcp_path=mcp_path,
        )
    install_request_body_limit(
        app, max_bytes=active_settings.max_http_request_bytes
    )
    if active_settings.auth_mode != "none":
        app.add_middleware(AuthMiddleware, public_routes=public_routes)
    return app


def build_mcp_http_app(
    mcp: FastMCP,
    *,
    runtime: ControlRuntime | None = None,
) -> Starlette:
    """Use the MCP SDK's HTTP app and add local public routes/auth."""
    settings = (
        runtime.legacy_settings if runtime is not None else get_settings()
    )
    if hasattr(mcp, "streamable_http_app"):
        inner: Starlette = mcp.streamable_http_app()
        session_manager = getattr(mcp, "_session_manager", None)
        if session_manager is not None and not bool(
            getattr(session_manager, "stateless", False)
        ):
            idle_timeout_s = max(1, settings.mcp_session_idle_timeout_s)
            session_manager.session_idle_timeout = idle_timeout_s
            maximum_tool_watchdog_s = tool_timeout_s("bash")
            if idle_timeout_s <= maximum_tool_watchdog_s:
                audit(
                    "mcp_session_idle_timeout_risk",
                    idle_timeout_s=idle_timeout_s,
                    maximum_tool_watchdog_s=maximum_tool_watchdog_s,
                )
        mcp_settings = getattr(mcp, "settings", None)
        return _build_authenticated_mcp_http_app(
            inner,
            session_manager=session_manager,
            mcp_path=str(getattr(mcp_settings, "streamable_http_path", "/mcp")),
            settings=settings,
            runtime=runtime,
        )
    if hasattr(mcp, "sse_app"):
        inner = mcp.sse_app()
        return _build_authenticated_mcp_http_app(
            inner,
            settings=settings,
            runtime=runtime,
        )
    raise RuntimeError(
        "MCP HTTP ASGI app not available since both streamable_http_app and sse_app are not available"
    )


def run_mcp(
    *,
    tool_catalog: ToolCatalog | None = None,
    runtime: ControlRuntime | None = None,
) -> None:
    """Start MCP with one control runtime owner over stdio or HTTP."""
    settings = (
        runtime.legacy_settings if runtime is not None else get_settings()
    )
    active_runtime = runtime or build_control_runtime(settings)
    mode = active_runtime.config.mode
    if mode != "stdio":
        validate_public_oauth_configuration(settings)
    mcp = build_mcp(
        tool_catalog=tool_catalog,
        runtime=active_runtime,
        own_runtime_lifespan=mode == "stdio",
    )

    if mode == "stdio":
        # stdio mode talks directly to the parent process; no HTTP app is needed.
        mcp.run(transport="stdio")
    else:
        app = build_mcp_http_app(mcp, runtime=active_runtime)
        uvicorn.run(
            app,
            host=active_runtime.config.host,
            port=active_runtime.config.port,
        )
