"""Build the HTTP debug host for the canonical MCP tool surface."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent
from starlette.routing import BaseRoute

from ... import __version__
from ...config.control import resolve_control_config
from ...config.settings import get_settings
from ...http.request_limits import install_request_body_limit
from ...oauth.core.security import validate_public_oauth_configuration
from ...oauth.http.middleware import AuthMiddleware
from ...persistence import FileStateStore
from ...protocol.executor import EXECUTOR_TRANSFER_PREFIX
from ...tools.catalog import ToolCatalog
from ..execution_context import ControlExecutionContextMiddleware
from ..mcp.app import build_mcp
from ..runtime import ControlRuntime, build_control_runtime
from ..server import run_uvicorn
from .errors import install_error_handlers
from .shared_routes import control_http_routes


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


def _control_runtime_lifespan(runtime: ControlRuntime):
    """Build the HTTP host lifespan for one control runtime owner."""

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
    core_routes, ui_routes, public_routes = control_http_routes(
        settings, runtime
    )
    app.router.routes.extend(core_routes)
    public_routes.extend(_fastapi_documentation_routes(app))
    mcp = build_mcp(tool_catalog=catalog, runtime=runtime)

    @app.get("/tools")
    async def list_debug_tools(response: Response) -> dict[str, object]:
        response.headers["Cache-Control"] = "no-store"
        return {
            "tools": [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in await mcp.list_tools()
            ]
        }

    @app.post("/tools/{tool_name}")
    async def call_debug_tool(
        tool_name: str,
        arguments: dict[str, object] | None = None,
    ) -> JSONResponse:
        try:
            result = await mcp.call_tool(tool_name, arguments or {})
        except ToolError as exc:
            result = CallToolResult(
                is_error=True, content=[TextContent(type="text", text=str(exc))]
            )
        except MCPError as exc:
            # Protocol errors are not tool results; preserve the SDK error data.
            return JSONResponse(
                status_code=400,
                content=exc.error.model_dump(mode="json", exclude_none=True),
            )
        return JSONResponse(
            result.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    app.router.routes.extend(ui_routes)
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
