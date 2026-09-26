from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import workgate.control.http.app as http_app
import workgate.control.mcp.app as mcp_app
from workgate.config.settings import Settings, configure_settings
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp, build_mcp_http_app
from workgate.control.runtime import build_control_runtime
from workgate.persistence import (
    FileStateStore,
    get_state_store,
    use_state_store,
)


def _outer_state_store(settings: Settings) -> FileStateStore:
    return FileStateStore(lambda: settings.state_dir.parent / "outer-state")


def test_rest_http_host_owns_control_runtime_lifespan(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="http",
        auth_mode="none",
    )
    configure_settings(settings)
    outer_state_store = _outer_state_store(settings)
    runtime = build_control_runtime(settings)
    with use_state_store(outer_state_store):
        app = build_http_app(runtime=runtime)
        assert get_state_store() is outer_state_store
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert get_state_store() is outer_state_store
        assert get_state_store() is outer_state_store


@pytest.mark.parametrize("mode", ["default", "catalog", "runtime"])
def test_run_http_owns_runtime_for_compatibility_and_explicit_paths(
    tmp_path, monkeypatch, mode
):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="http",
        auth_mode="none",
        host="127.0.0.1",
        port=8765,
    )
    configure_settings(settings)
    runtime = build_control_runtime(settings)
    catalog = runtime.tool_catalog
    app = object()
    calls = []

    def build_runtime(configured_settings):
        calls.append(("runtime", configured_settings))
        return runtime

    def build(**kwargs):
        calls.append(("build", kwargs))
        return app

    monkeypatch.setattr(http_app, "build_control_runtime", build_runtime)
    monkeypatch.setattr(http_app, "build_http_app", build)
    monkeypatch.setattr(
        http_app.uvicorn,
        "run",
        lambda built_app, *, host, port: calls.append(
            ("uvicorn", built_app, host, port)
        ),
    )

    if mode == "default":
        http_app.run_http()
        expected_calls = [
            ("runtime", settings),
            ("build", {"tool_catalog": None, "runtime": runtime}),
        ]
    elif mode == "catalog":
        http_app.run_http(tool_catalog=catalog)
        expected_calls = [
            ("runtime", settings),
            ("build", {"tool_catalog": catalog, "runtime": runtime}),
        ]
    else:
        http_app.run_http(runtime=runtime)
        expected_calls = [
            ("build", {"tool_catalog": None, "runtime": runtime}),
        ]

    assert calls == [
        *expected_calls,
        ("uvicorn", app, "127.0.0.1", 8765),
    ]


@pytest.mark.asyncio
async def test_stdio_fastmcp_server_run_lifespan_owns_control_runtime(
    tmp_path,
):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="stdio",
        auth_mode="none",
    )
    configure_settings(settings)
    outer_state_store = _outer_state_store(settings)
    runtime = build_control_runtime(settings)
    with use_state_store(outer_state_store):
        mcp = build_mcp(runtime=runtime, own_runtime_lifespan=True)
        assert get_state_store() is outer_state_store
        async with mcp._mcp_server.lifespan(mcp._mcp_server):
            assert get_state_store() is outer_state_store
        assert get_state_store() is outer_state_store


@pytest.mark.asyncio
async def test_mcp_http_sessions_do_not_own_process_runtime(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="mcp",
        auth_mode="none",
    )
    configure_settings(settings)
    outer_state_store = _outer_state_store(settings)
    runtime = build_control_runtime(settings)
    with use_state_store(outer_state_store):
        mcp = build_mcp(runtime=runtime)
        async with mcp._mcp_server.lifespan(mcp._mcp_server):
            assert get_state_store() is outer_state_store


def test_mcp_http_host_owns_control_runtime_once(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="mcp",
        auth_mode="none",
    )
    configure_settings(settings)
    outer_state_store = _outer_state_store(settings)
    runtime = build_control_runtime(settings)
    with use_state_store(outer_state_store):
        mcp = build_mcp(runtime=runtime)
        app = build_mcp_http_app(mcp, runtime=runtime)
        assert get_state_store() is outer_state_store
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert get_state_store() is outer_state_store
        assert get_state_store() is outer_state_store


def test_mcp_http_inner_startup_failure_closes_control_runtime(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="mcp",
        auth_mode="none",
    )
    configure_settings(settings)
    outer_state_store = _outer_state_store(settings)
    runtime = build_control_runtime(settings)

    @asynccontextmanager
    async def failing_sdk_lifespan(
        _app: Starlette,
    ) -> AsyncGenerator[None]:
        assert get_state_store() is outer_state_store
        raise RuntimeError("sdk startup failed")
        yield

    inner = Starlette(lifespan=failing_sdk_lifespan)
    app, _public_routes = mcp_app._add_public_routes_to_mcp_http_app(
        inner, runtime=runtime
    )
    with use_state_store(outer_state_store):
        with (
            pytest.raises(RuntimeError, match="sdk startup failed"),
            TestClient(app),
        ):
            pytest.fail("inner startup failure must prevent serving")
        assert get_state_store() is outer_state_store
