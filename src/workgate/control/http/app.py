"""Build the FastAPI REST HTTP application."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.routing import BaseRoute

from ... import __version__
from ...config.control import ControlSettingsView
from ...config.settings import get_settings
from ...http.public_routes import public_http_routes
from ...http.request_limits import install_request_body_limit
from ...oauth.core.security import validate_public_oauth_configuration
from ...oauth.http.middleware import AuthMiddleware
from ...oauth.http.routes import oauth_public_routes
from ...tools.catalog import ToolCatalog
from ...ui.http.routes import UI_API_PREFIX, human_ui_routes
from ..runtime import ControlRuntime, build_control_runtime
from .errors import install_error_handlers
from .executor_admin import executor_admin_routes
from .executor_routes import executor_routes
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


def _install_public_routes(
    app: FastAPI,
    settings: ControlSettingsView,
    *,
    runtime: ControlRuntime | None = None,
) -> list[BaseRoute]:
    """Install public non-tool routes for the REST app. It returns public routes for oauth usage."""
    documentation_routes = _fastapi_documentation_routes(app)
    installed_routes = [
        *public_http_routes(),
        *(
            executor_routes(
                runtime.executor_transport, runtime.executor_pairing
            )
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
    """Construct the authenticated REST API from one explicit tool catalog."""
    if runtime is None and tool_catalog is None:
        runtime = build_control_runtime(get_settings())
    settings = runtime.config if runtime is not None else get_settings()
    if tool_catalog is not None:
        catalog = tool_catalog
    elif runtime is not None:
        catalog = runtime.tool_catalog
    else:  # pragma: no cover - guarded above; keeps the invariant explicit.
        raise RuntimeError(
            "control HTTP requires a routed runtime or explicit catalog"
        )

    app = FastAPI(
        title="workgate REST API",
        version=__version__,
        lifespan=(
            _control_runtime_lifespan(runtime) if runtime is not None else None
        ),
    )

    app.state.control_runtime = runtime
    install_error_handlers(app)
    install_tools_timeout_middleware(app, catalog)
    public_routes = _install_public_routes(app, settings, runtime=runtime)
    register_http_tool_routes(app, catalog)
    ui_routes, ui_public_routes = human_ui_routes(settings)
    app.router.routes.extend(ui_routes)
    public_routes.extend(ui_public_routes)
    if runtime is not None:
        app.router.routes.extend(
            executor_admin_routes(
                runtime.control_state,
                runtime.executor_transport,
                runtime.executor_pairing,
                api_prefix=UI_API_PREFIX,
            )
        )
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
    if runtime is None:
        source_settings = get_settings()
        active_runtime = build_control_runtime(source_settings)
    else:
        active_runtime = runtime
    validate_public_oauth_configuration(active_runtime.config)
    app = build_http_app(
        tool_catalog=tool_catalog,
        runtime=active_runtime,
    )
    uvicorn.run(
        app, host=active_runtime.config.host, port=active_runtime.config.port
    )
