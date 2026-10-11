"""Build the HTTP debug host for the canonical MCP tool surface."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from starlette.routing import BaseRoute

from ... import __version__
from ...config.control import ControlConfig, resolve_control_config
from ...config.settings import get_settings
from ...http.public_routes import public_http_routes
from ...http.request_limits import install_request_body_limit
from ...oauth.core.security import validate_public_oauth_configuration
from ...oauth.http.middleware import AuthMiddleware
from ...oauth.http.routes import oauth_public_routes
from ...persistence import FileStateStore
from ...protocol.executor import EXECUTOR_TRANSFER_PREFIX
from ...tools.catalog import ToolCatalog
from ...ui.http.routes import UI_API_PREFIX, human_ui_routes
from ..execution_context import ControlExecutionContextMiddleware
from ..executor_bootstrap import executor_bootstrap_routes
from ..mcp.app import build_mcp
from ..runtime import ControlRuntime, build_control_runtime
from ..server import run_uvicorn
from .errors import install_error_handlers
from .executor_admin import executor_admin_routes
from .executor_routes import executor_routes
from .stream_routes import terminal_stream_routes


def _fastapi_documentation_routes(app: FastAPI) -> list[BaseRoute]:
    """Return FastAPI-generated documentation routes that should stay public."""
    public_paths = {
        path
        for path in (
            app.docs_url,
            app.redoc_url,
            app.openapi_url,
            app.swagger_ui_oauth2_redirect_url,
        )
        if path is not None
    }
    return [
        route
        for route in app.router.routes
        if getattr(route, "path", None) in public_paths
    ]


def _install_public_routes(
    app: FastAPI,
    settings: ControlConfig,
    *,
    runtime: ControlRuntime | None = None,
) -> list[BaseRoute]:
    """Install non-tool HTTP routes and identify paths exempt from OAuth."""
    documentation_routes = _fastapi_documentation_routes(app)
    installed_routes = [
        *public_http_routes(settings),
        *(
            [
                *executor_routes(
                    runtime.executor_transport, runtime.executor_pairing
                ),
                *executor_bootstrap_routes(settings.resolved_base_url),
                *runtime.transfer_gateway.routes(),
                *terminal_stream_routes(
                    runtime.executor_transport, runtime.stream_hub
                ),
            ]
            if runtime is not None
            else ()
        ),
        *oauth_public_routes(),
    ]
    app.router.routes.extend(installed_routes)
    return [*documentation_routes, *installed_routes]


def _control_runtime_lifespan(runtime: ControlRuntime):
    """Build the REST host lifespan for one control runtime owner."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        async with runtime.lifespan():
            yield

    return lifespan


def build_http_app(
    *,
    tool_catalog: ToolCatalog | None = None,
    runtime: ControlRuntime | None = None,
) -> FastAPI:
    """Expose registered MCP tools through one thin authenticated debug API."""
    if runtime is None and tool_catalog is None:
        runtime = build_control_runtime(get_settings())
    settings = (
        runtime.config
        if runtime is not None
        else resolve_control_config(get_settings())
    )
    if tool_catalog is not None:
        catalog = tool_catalog
    elif runtime is not None:
        catalog = runtime.tool_catalog
    else:  # pragma: no cover - guarded above; keeps the invariant explicit.
        raise RuntimeError(
            "control HTTP requires a routed runtime or explicit catalog"
        )

    app = FastAPI(
        title="workgate debug HTTP API",
        version=__version__,
        lifespan=(
            _control_runtime_lifespan(runtime) if runtime is not None else None
        ),
    )

    app.state.control_runtime = runtime
    install_error_handlers(app)
    public_routes = _install_public_routes(app, settings, runtime=runtime)
    mcp = build_mcp(tool_catalog=catalog, runtime=runtime)

    @app.get("/tools")
    async def list_debug_tools() -> dict[str, object]:
        return {
            "tools": [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in await mcp.list_tools()
            ]
        }

    @app.post("/tools/{tool_name}")
    async def call_debug_tool(
        tool_name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        try:
            result = await mcp.call_tool(tool_name, arguments)
        except ToolError as exc:
            result = CallToolResult(
                is_error=True, content=[TextContent(type="text", text=str(exc))]
            )
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    ui_routes, ui_public_routes = human_ui_routes(settings)
    app.router.routes.extend(ui_routes)
    public_routes.extend(ui_public_routes)
    if runtime is not None:
        app.router.routes.extend(
            executor_admin_routes(
                runtime.executor_fleet,
                runtime.executor_pairing,
                api_prefix=UI_API_PREFIX,
            )
        )
    install_request_body_limit(
        app,
        max_bytes=settings.max_http_request_bytes,
        streaming_prefixes=(
            (EXECUTOR_TRANSFER_PREFIX,) if runtime is not None else ()
        ),
    )
    if settings.auth_mode != "none":
        app.add_middleware(AuthMiddleware, public_routes=public_routes)
    state_store = (
        runtime.state_store
        if runtime is not None
        else FileStateStore(lambda: settings.state_dir)
    )
    app.add_middleware(
        ControlExecutionContextMiddleware,
        config=settings,
        state_store=state_store,
        oauth_state=None if runtime is None else runtime.oauth_state,
        managed_jobs_runtime=(
            None if runtime is None else runtime.managed_jobs_runtime
        ),
    )
    return app


def run_http(
    *,
    runtime: ControlRuntime,
    tool_catalog: ToolCatalog | None = None,
) -> None:
    """Run debug HTTP with one explicit control runtime owner."""
    validate_public_oauth_configuration(runtime.config)
    app = build_http_app(tool_catalog=tool_catalog, runtime=runtime)
    run_uvicorn(app, config=runtime.config)
