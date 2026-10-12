from collections.abc import Iterable

import pytest
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute
from starlette.testclient import TestClient

import workgate.control.http.app as http_app
import workgate.control.mcp.app as mcp_app
from workgate.config.control import resolve_control_config
from workgate.config.settings import Settings, configure_settings
from workgate.control.runtime import build_control_runtime
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
    settings = Settings(mode="http", auth_mode="none")
    configure_settings(settings)

    shared_routes = public_http_routes(resolve_control_config(settings))
    shared_signatures = _route_signatures(shared_routes)
    assert shared_signatures == _SHARED_PUBLIC_ROUTE_SIGNATURES

    rest_app = http_app.build_http_app()
    mcp_http_app, mcp_public_routes = (
        mcp_app._add_public_routes_to_mcp_http_app(Starlette())
    )
    rest_public_routes = rest_app.routes

    shared_count = len(shared_signatures)
    for signature in shared_signatures:
        assert _route_signatures(rest_public_routes).count(signature) == 1
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
        ui_enabled=True,
    )
    configure_settings(settings)

    expected_routes, _expected_public_routes = human_ui_routes(
        resolve_control_config(settings)
    )
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


def _all_http_signatures(
    routes: Iterable[BaseRoute],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Compare externally routed HTTP and WebSocket endpoints, not helpers."""
    return tuple(
        (
            route.path,
            ("WEBSOCKET",)
            if isinstance(route, WebSocketRoute)
            else tuple(sorted(route.methods or ())),
        )
        for route in routes
        if isinstance(route, (Route, WebSocketRoute))
    )


def test_runtime_rest_and_mcp_share_non_tool_routes(tmp_path) -> None:
    """Both hosts expose the same executor, OAuth and WebUI HTTP contracts."""
    settings = Settings(
        mode="mcp",
        auth_mode="oauth",
        base_url="https://control.example.test",
        default_workdir=tmp_path / "workspace",
        state_dir=tmp_path / "state",
    )
    configure_settings(settings)
    runtime = build_control_runtime(settings)

    rest = http_app.build_http_app(runtime=runtime)
    mcp_host, exempt_routes = mcp_app._add_public_routes_to_mcp_http_app(
        Starlette(), runtime=runtime
    )

    rest_routes = _all_http_signatures(rest.routes)
    rest_shared = tuple(
        (path, methods)
        for path, methods in rest_routes
        if path
        not in {
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            "/tools",
            "/tools/{tool_name}",
        }
    )
    mcp_shared = _all_http_signatures(mcp_host.routes)
    assert len(rest_shared) == len(set(rest_shared))
    assert len(mcp_shared) == len(set(mcp_shared))
    assert set(rest_shared) == set(mcp_shared)

    paths = {path for path, _methods in mcp_shared}
    assert {
        "/healthz",
        "/readyz",
        "/oauth/token",
        "/pair",
        "/api/ui/executors",
    } <= paths
    assert any(path.startswith("/executor/v1/") for path in paths)
    assert any(methods == ("WEBSOCKET",) for _, methods in mcp_shared)

    # Authentication exemptions are endpoint-specific, not a broad /api bypass.
    exempt = {signature for signature in _all_http_signatures(exempt_routes)}
    assert ("/healthz", ("GET", "HEAD")) in exempt
    assert not any(path == "/api/ui/executors" for path, _ in exempt)
    assert not any(path == "/tools" for path, _ in exempt)
    assert isinstance(mcp_host.routes[-1], Mount)


@pytest.mark.parametrize("mode", ["rest", "mcp"])
def test_oauth_boundary_on_shared_control_routes(tmp_path, mode: str) -> None:
    settings = Settings(
        mode="mcp",
        auth_mode="oauth",
        base_url="https://control.example.test",
        default_workdir=tmp_path / "workspace",
        state_dir=tmp_path / "state",
    )
    configure_settings(settings)
    runtime = build_control_runtime(settings)
    if mode == "rest":
        app = http_app.build_http_app(runtime=runtime)
    else:
        app = mcp_app._build_authenticated_mcp_http_app(
            Starlette(), runtime=runtime
        )

    with TestClient(
        app,
        base_url="https://control.example.test",
        client=("203.0.113.10", 50000),
    ) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/pair").status_code == 200
        assert client.get("/ui").status_code == 200
        assert client.get("/api/ui/executors").status_code == 401
