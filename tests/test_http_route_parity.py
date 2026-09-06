from collections.abc import Iterable

from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Route

import workgate.control.http.app as http_app
import workgate.control.mcp.app as mcp_app
from workgate.config.settings import Settings, configure_settings
from workgate.http.public_routes import public_http_routes
from workgate.ui.http.routes import human_ui_routes

_SHARED_PUBLIC_ROUTE_SIGNATURES = (
    ("/healthz", ("GET", "HEAD")),
    ("/readyz", ("GET", "HEAD")),
    ("/version", ("GET", "HEAD")),
    ("/download/{token}", ("GET", "HEAD")),
)


def _route_signature(route: BaseRoute) -> tuple[str, tuple[str, ...]]:
    assert isinstance(route, Route)
    return route.path, tuple(sorted(route.methods or ()))


def _route_signatures(
    routes: Iterable[BaseRoute],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        _route_signature(route) for route in routes if isinstance(route, Route)
    )


def _route_paths(routes: Iterable[BaseRoute]) -> tuple[str, ...]:
    return tuple(str(getattr(route, "path", "")) for route in routes)


def test_shared_public_routes_have_rest_and_mcp_http_parity() -> None:
    settings = Settings(mode="http", auth_mode="none", remote_enabled=False)
    configure_settings(settings)

    shared_routes = public_http_routes(
        settings,
        readyz_include_workspace_root=False,
    )
    shared_signatures = _route_signatures(shared_routes)
    assert shared_signatures == _SHARED_PUBLIC_ROUTE_SIGNATURES

    rest_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    rest_public_routes = http_app._install_public_routes(rest_app, settings)

    mcp_http_app, mcp_public_routes = (
        mcp_app._add_public_routes_to_mcp_http_app(Starlette())
    )

    shared_count = len(shared_signatures)
    assert (
        _route_signatures(rest_public_routes)[:shared_count]
        == shared_signatures
    )
    assert (
        _route_signatures(mcp_public_routes)[:shared_count] == shared_signatures
    )

    rest_installed = set(_route_signatures(rest_app.routes))
    mcp_installed = set(_route_signatures(mcp_http_app.routes))
    assert set(shared_signatures) <= rest_installed
    assert set(shared_signatures) <= mcp_installed


def test_human_ui_routes_have_rest_and_mcp_http_parity() -> None:
    settings = Settings(
        mode="mcp",
        auth_mode="none",
        remote_enabled=False,
        ui_enabled=True,
    )
    configure_settings(settings)

    expected_routes, _expected_public_routes = human_ui_routes(settings)
    expected_paths = _route_paths(expected_routes)
    rest_paths = _route_paths(http_app.build_http_app().routes)
    mcp_http_app, _mcp_public_routes = (
        mcp_app._add_public_routes_to_mcp_http_app(Starlette())
    )
    mcp_paths = _route_paths(mcp_http_app.routes)
    mcp_mount_index = len(mcp_paths) - 1

    assert mcp_paths[mcp_mount_index] == ""
    for path in expected_paths:
        assert rest_paths.count(path) == 1
        assert mcp_paths.count(path) == 1
        assert mcp_paths.index(path) < mcp_mount_index
