"""Build and run the MCP server."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount

from ...audit import audit
from ...config.control import ControlSettingsView
from ...config.settings import get_settings
from ...http.public_routes import public_http_routes
from ...http.request_limits import install_request_body_limit
from ...oauth.core.security import validate_public_oauth_configuration
from ...oauth.http.middleware import AuthMiddleware
from ...oauth.http.routes import oauth_public_routes
from ...tools.catalog import ToolCatalog
from ...tools.contracts import McpToolContext
from ...tools.metadata import install_tool_safety_annotations
from ...ui.http.routes import UI_API_PREFIX, human_ui_routes
from ..http.executor_admin import executor_admin_routes
from ..http.executor_routes import executor_routes
from ..runtime import ControlRuntime, build_control_runtime
from ..tool_timeouts import tool_timeout_s
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
    """Create the MCP server from a routed control runtime or explicit catalog."""
    auto_runtime = runtime is None and tool_catalog is None
    if auto_runtime:
        runtime = build_control_runtime(get_settings())
    settings = runtime.config if runtime is not None else get_settings()
    if tool_catalog is not None:
        catalog = tool_catalog
    elif runtime is not None:
        catalog = runtime.tool_catalog
    else:  # pragma: no cover - guarded above.
        raise RuntimeError(
            "control MCP requires a routed runtime or explicit catalog"
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
    cast(Any, mcp)._workgate_runtime = runtime
    cast(Any, mcp)._workgate_runtime_lifespan_owned = own_runtime_lifespan
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
    settings: ControlSettingsView | None = None,
    runtime: ControlRuntime | None = None,
) -> tuple[Starlette, list[BaseRoute]]:
    """Serve health/OAuth routes directly and send everything else to MCP."""
    active_settings = (
        settings
        if settings is not None
        else runtime.config
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
        *public_http_routes(settings),
        *(
            executor_routes(
                runtime.executor_transport,
                runtime.executor_pairing,
            )
            if runtime is not None
            else ()
        ),
        *oauth_public_routes(),
    ]
    ui_routes, ui_public_routes = human_ui_routes(active_settings)
    executor_admin = (
        executor_admin_routes(
            runtime.control_state,
            runtime.executor_transport,
            runtime.executor_pairing,
            api_prefix=UI_API_PREFIX,
        )
        if runtime is not None
        else ()
    )
    routes = [
        *public_routes,
        *ui_routes,
        *executor_admin,
        Mount("/", app=mcp_app),
    ]
    public_routes.extend(ui_public_routes)
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.control_runtime = runtime
    return app, public_routes


def _build_authenticated_mcp_http_app(
    mcp_app: Starlette,
    *,
    session_manager: object | None = None,
    mcp_path: str = "/mcp",
    settings: ControlSettingsView | None = None,
    runtime: ControlRuntime | None = None,
) -> Starlette:
    """Add resource limits and OAuth protection around the MCP HTTP app."""
    active_settings = (
        settings
        if settings is not None
        else runtime.config
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
    active_runtime = runtime
    if active_runtime is None and not bool(
        getattr(mcp, "_workgate_runtime_lifespan_owned", False)
    ):
        active_runtime = cast(
            ControlRuntime | None, getattr(mcp, "_workgate_runtime", None)
        )
    settings = (
        active_runtime.config if active_runtime is not None else get_settings()
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
            runtime=active_runtime,
        )
    if hasattr(mcp, "sse_app"):
        inner = mcp.sse_app()
        return _build_authenticated_mcp_http_app(
            inner,
            settings=settings,
            runtime=active_runtime,
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
    if runtime is None:
        source_settings = get_settings()
        active_runtime = build_control_runtime(source_settings)
    else:
        active_runtime = runtime
    mode = active_runtime.config.mode
    if mode != "stdio":
        validate_public_oauth_configuration(active_runtime.config)
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
