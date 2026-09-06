from types import SimpleNamespace
from typing import Any, cast

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import workgate.control.mcp.app as mcp_app
from workgate.config.settings import Settings, configure_settings
from workgate.control.mcp.session_limits import (
    McpSessionLimitMiddleware,
)
from workgate.http.request_limits import RequestBodyLimitMiddleware
from workgate.oauth.http.middleware import AuthMiddleware
from workgate.ui.security import (
    UI_LOCAL_TOKEN_HEADER,
    get_or_create_ui_local_token,
)


async def _ok(request):
    return None


class _DummyMcp:
    def __init__(self):
        self.transports = []
        self._session_manager: Any = None
        self.settings: Any = SimpleNamespace(streamable_http_path="/mcp")

    def streamable_http_app(self):
        return Starlette(routes=[Route("/mcp", _ok)])

    def run(self, *, transport: str):
        self.transports.append(transport)


class _DummySseMcp:
    def sse_app(self):
        return Starlette(routes=[Route("/sse", _ok)])


class _EmptyCatalog:
    def register_mcp(self, mcp, context) -> None:
        del mcp, context


def _runtime_stub(settings: Settings, tool_catalog: object | None = None):
    return SimpleNamespace(
        config=SimpleNamespace(
            mode=settings.mode,
            host=settings.host,
            port=settings.port,
        ),
        legacy_settings=settings,
        tool_catalog=tool_catalog,
    )


def _route_paths(app: Starlette) -> list[str]:
    return [getattr(route, "path", "") for route in app.routes]


def test_build_mcp_http_app_wraps_mcp_with_oauth_route_app():
    configure_settings(
        Settings(mode="mcp", auth_mode="none", remote_enabled=False)
    )

    app = mcp_app.build_mcp_http_app(cast(Any, _DummyMcp()))

    assert app is not None
    paths = _route_paths(app)
    assert paths[:2] == ["/healthz", "/readyz"]
    assert "/download/{token}" in paths
    assert "/.well-known/oauth-protected-resource" in paths
    assert paths.index("/download/{token}") < paths.index(
        "/.well-known/oauth-protected-resource"
    )
    assert "/oauth/token" in paths
    assert "/ui" in paths
    assert "/api/ui/bootstrap" in paths
    assert paths.index("/ui") < len(paths) - 1
    assert paths.index("/api/ui/bootstrap") < len(paths) - 1
    assert paths[-1] == ""


def test_mcp_http_app_serves_public_ui_and_native_tui_api(tmp_path):
    configure_settings(
        Settings(
            mode="mcp",
            auth_mode="oauth",
            remote_enabled=False,
            base_url="http://127.0.0.1:8765",
            state_dir=tmp_path,
            ui_enabled=True,
        )
    )

    app = mcp_app.build_mcp_http_app(cast(Any, _DummyMcp()))
    client = TestClient(app, client=("127.0.0.1", 4242))

    page = client.get("/ui")
    unauthenticated_api = client.get("/api/ui/bootstrap")
    token = get_or_create_ui_local_token()
    native_api = client.get(
        "/api/ui/bootstrap",
        headers={UI_LOCAL_TOKEN_HEADER: token},
    )

    assert page.status_code == 200
    assert "workgate" in page.text
    assert unauthenticated_api.status_code == 401
    assert native_api.status_code == 200
    assert native_api.json()["data"]["machines"][0]["name"] == "local"


def test_build_mcp_http_app_supports_sdk_sse_fallback():
    configure_settings(
        Settings(mode="mcp", auth_mode="none", remote_enabled=False)
    )

    app = mcp_app.build_mcp_http_app(cast(Any, _DummySseMcp()))

    assert app is not None
    assert _route_paths(app)[-1] == ""


def test_build_mcp_http_app_includes_remote_routes_when_enabled():
    configure_settings(
        Settings(mode="mcp", auth_mode="none", remote_enabled=True)
    )

    app = mcp_app.build_mcp_http_app(cast(Any, _DummyMcp()))

    assert app is not None
    paths = _route_paths(app)
    assert "/join" in paths
    assert "/remote/register" in paths
    assert "/remote/poll" in paths


def test_build_mcp_http_app_uses_explicit_runtime_settings_not_ambient():
    configure_settings(
        Settings(
            mode="mcp",
            auth_mode="none",
            remote_enabled=False,
            mcp_max_sessions=2,
            max_http_request_bytes=100,
            mcp_session_idle_timeout_s=60,
        )
    )
    runtime_settings = Settings(
        mode="mcp",
        auth_mode="oauth",
        remote_enabled=True,
        remote_http_transfer_enabled=False,
        base_url="https://runtime.example",
        mcp_max_sessions=17,
        max_http_request_bytes=4321,
        mcp_session_idle_timeout_s=987,
    )
    runtime = cast(Any, _runtime_stub(runtime_settings))
    session_manager = SimpleNamespace(
        stateless=False,
        session_idle_timeout=1,
    )
    dummy = _DummyMcp()
    dummy._session_manager = session_manager
    dummy.settings = SimpleNamespace(streamable_http_path="/mcp")

    app = mcp_app.build_mcp_http_app(cast(Any, dummy), runtime=runtime)

    paths = _route_paths(app)
    assert "/join" in paths
    assert "/remote/register" in paths
    assert session_manager.session_idle_timeout == 987

    assert any(entry.cls is AuthMiddleware for entry in app.user_middleware)
    session_limit = next(
        entry
        for entry in app.user_middleware
        if entry.cls is McpSessionLimitMiddleware
    )
    request_limit = next(
        entry
        for entry in app.user_middleware
        if entry.cls is RequestBodyLimitMiddleware
    )
    assert session_limit.kwargs["max_sessions"] == 17
    assert request_limit.kwargs["max_bytes"] == 4321


def test_build_mcp_uses_runtime_settings_for_transport_security():
    configure_settings(
        Settings(
            mode="mcp",
            auth_mode="none",
            base_url="https://ambient.example",
        )
    )
    runtime_settings = Settings(
        mode="mcp",
        auth_mode="none",
        base_url="https://runtime.example",
    )
    runtime = cast(Any, _runtime_stub(runtime_settings, _EmptyCatalog()))

    mcp = mcp_app.build_mcp(runtime=runtime)

    security = mcp.settings.transport_security
    assert security is not None
    assert "runtime.example" in security.allowed_hosts
    assert "https://runtime.example" in security.allowed_origins
    assert "ambient.example" not in security.allowed_hosts
    assert "https://ambient.example" not in security.allowed_origins


def test_run_mcp_uses_runtime_owned_stdio_transport(monkeypatch):
    settings = Settings(mode="stdio", auth_mode="none")
    configure_settings(settings)
    runtime = cast(Any, _runtime_stub(settings, object()))
    dummy = _DummyMcp()
    calls = []

    def build_runtime(configured_settings):
        calls.append(("runtime", configured_settings))
        return runtime

    def build(*, tool_catalog=None, runtime=None, own_runtime_lifespan=False):
        calls.append(("build", tool_catalog, runtime, own_runtime_lifespan))
        return dummy

    monkeypatch.setattr(mcp_app, "build_control_runtime", build_runtime)
    monkeypatch.setattr(mcp_app, "build_mcp", build)

    mcp_app.run_mcp()

    assert calls == [
        ("runtime", settings),
        ("build", None, runtime, True),
    ]
    assert dummy.transports == ["stdio"]


def test_run_mcp_stdio_runtime_owns_fastmcp_lifespan(monkeypatch):
    runtime = cast(
        Any,
        _runtime_stub(Settings(mode="stdio", auth_mode="none"), object()),
    )
    dummy = _DummyMcp()
    calls = []

    def build(*, tool_catalog=None, runtime=None, own_runtime_lifespan=False):
        calls.append(("build", tool_catalog, runtime, own_runtime_lifespan))
        return dummy

    monkeypatch.setattr(mcp_app, "build_mcp", build)

    mcp_app.run_mcp(runtime=runtime)

    assert calls == [("build", None, runtime, True)]
    assert dummy.transports == ["stdio"]


def test_run_mcp_http_runtime_is_owned_by_outer_asgi_lifespan(monkeypatch):
    runtime = cast(
        Any,
        _runtime_stub(
            Settings(
                mode="mcp",
                auth_mode="none",
                host="127.0.0.1",
                port=8765,
            ),
            object(),
        ),
    )
    dummy = _DummyMcp()
    app = object()
    calls = []

    def build(*, tool_catalog=None, runtime=None, own_runtime_lifespan=False):
        calls.append(("build", tool_catalog, runtime, own_runtime_lifespan))
        return dummy

    def build_http(mcp, *, runtime=None):
        calls.append(("http", mcp, runtime))
        return app

    monkeypatch.setattr(mcp_app, "build_mcp", build)
    monkeypatch.setattr(mcp_app, "build_mcp_http_app", build_http)
    monkeypatch.setattr(
        mcp_app.uvicorn,
        "run",
        lambda built_app, *, host, port: calls.append(
            ("uvicorn", built_app, host, port)
        ),
    )

    mcp_app.run_mcp(runtime=runtime)

    assert calls == [
        ("build", None, runtime, False),
        ("http", dummy, runtime),
        ("uvicorn", app, "127.0.0.1", 8765),
    ]


def test_oauth_challenge_metadata_url_matches_rfc9728_path_resource():
    configure_settings(
        Settings(
            mode="mcp",
            auth_mode="oauth",
            remote_enabled=False,
            base_url="https://workgate.example.com",
        )
    )

    app = mcp_app.build_mcp_http_app(cast(Any, _DummyMcp()))
    client = TestClient(app)

    response = client.get("/mcp")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://workgate.example.com'
        '/.well-known/oauth-protected-resource/mcp"'
    )

    metadata = client.get("/.well-known/oauth-protected-resource/mcp")

    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "https://workgate.example.com/mcp"

    wrong_metadata = client.get("/.well-known/oauth-protected-resource/other")

    assert wrong_metadata.status_code == 404


async def _public_marker(request):
    return PlainTextResponse("public")


async def _private_marker(request):
    return PlainTextResponse("private")


def test_auth_middleware_uses_configured_public_route_matchers():
    configure_settings(
        Settings(
            mode="mcp",
            auth_mode="oauth",
            remote_enabled=False,
            base_url="https://workgate.example.com",
        )
    )
    public_route = Route("/extra/{name}", _public_marker, methods=["GET"])
    app = Starlette(
        routes=[
            public_route,
            Route("/private", _private_marker, methods=["GET"]),
        ]
    )
    app.add_middleware(AuthMiddleware, public_routes=[public_route])
    client = TestClient(app)

    assert client.get("/extra/value").status_code == 200
    assert client.get("/extra/value/nested").status_code == 401
    protected_response = client.get("/private")
    assert protected_response.status_code == 401
    assert "resource_metadata" in protected_response.headers["www-authenticate"]
