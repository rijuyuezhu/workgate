"""Build the FastAPI REST HTTP application."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.routing import BaseRoute

from ... import __version__
from ...config.settings import Settings, get_settings
from ...http.public_routes import public_http_routes
from ...http.request_limits import install_request_body_limit
from ...oauth.core.security import validate_public_oauth_configuration
from ...oauth.http.middleware import AuthMiddleware
from ...oauth.http.routes import oauth_public_routes
from ...remote.http import remote_routes
from ...remote.transfer_gateway import build_transfer_gateway_router
from ...tools.catalog import ToolCatalog, build_tool_catalog
from ...ui.http.routes import human_ui_routes
from ..runtime import ControlRuntime, build_control_runtime
from .errors import install_error_handlers
from .tool_routes import (
    install_tool_cache_control_middleware,
    install_tools_timeout_middleware,
    register_http_tool_routes,
)


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


def _install_public_routes(app: FastAPI, settings: Settings) -> list[BaseRoute]:
    """Install public non-tool routes for the REST app. It returns public routes for oauth usage."""
    documentation_routes = _fastapi_documentation_routes(app)
    installed_routes = [
        *public_http_routes(settings, readyz_include_workspace_root=False),
        *(remote_routes() if settings.remote_enabled else ()),
        *(
            build_transfer_gateway_router()
            if settings.remote_enabled and settings.remote_http_transfer_enabled
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
    """Construct the authenticated REST API from one explicit tool catalog."""
    settings = runtime.settings if runtime is not None else get_settings()
    catalog = tool_catalog or (
        runtime.tool_catalog
        if runtime is not None
        else build_tool_catalog(settings)
    )

    app = FastAPI(
        title="workgate REST API",
        version=__version__,
        lifespan=(
            _control_runtime_lifespan(runtime) if runtime is not None else None
        ),
    )

    install_error_handlers(app)
    install_tools_timeout_middleware(app, catalog)
    public_routes = _install_public_routes(app, settings)
    register_http_tool_routes(app, catalog)
    ui_routes, ui_public_routes = human_ui_routes(settings)
    app.router.routes.extend(ui_routes)
    public_routes.extend(ui_public_routes)
    install_request_body_limit(app, max_bytes=settings.max_http_request_bytes)
    if settings.auth_mode != "none":
        app.add_middleware(AuthMiddleware, public_routes=public_routes)
    install_tool_cache_control_middleware(app)
    return app


def run_http(
    *,
    tool_catalog: ToolCatalog | None = None,
    runtime: ControlRuntime | None = None,
) -> None:
    """Run the REST HTTP server with one control runtime owner."""
    settings = runtime.settings if runtime is not None else get_settings()
    validate_public_oauth_configuration(settings)
    active_runtime = runtime or build_control_runtime(settings)
    app = build_http_app(
        tool_catalog=tool_catalog,
        runtime=active_runtime,
    )
    uvicorn.run(app, host=settings.host, port=settings.port)
