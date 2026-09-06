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
    configure_state_store,
    get_state_store,
)
from workgate.tool_session import (
    configure_tool_session_store,
    get_tool_session_store,
)
from workgate.tool_session.store import ToolSessionStore


def _install_outer_stores(settings: Settings):
    outer_state_store = FileStateStore(
        lambda: settings.state_dir.parent / "outer-state"
    )
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    return outer_state_store, outer_session_store


def _assert_runtime_is_installed(runtime) -> None:
    assert get_state_store() is runtime.services.state_store
    assert get_tool_session_store() is runtime.services.tool_session_store


def _assert_outer_is_restored(outer_state_store, outer_session_store) -> None:
    assert get_state_store() is outer_state_store
    assert get_tool_session_store() is outer_session_store


def test_rest_http_host_owns_control_runtime_lifespan(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="http",
        auth_mode="none",
        remote_enabled=False,
    )
    configure_settings(settings)
    outer_state_store, outer_session_store = _install_outer_stores(settings)
    runtime = build_control_runtime(settings)
    try:
        app = build_http_app(runtime=runtime)
        _assert_outer_is_restored(outer_state_store, outer_session_store)

        with TestClient(app) as client:
            _assert_runtime_is_installed(runtime)
            assert client.get("/healthz").status_code == 200

        _assert_outer_is_restored(outer_state_store, outer_session_store)
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.parametrize("mode", ["default", "catalog", "runtime"])
def test_run_http_owns_runtime_for_compatibility_and_explicit_paths(
    tmp_path, monkeypatch, mode
):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="http",
        auth_mode="none",
        remote_enabled=False,
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
        remote_enabled=False,
    )
    configure_settings(settings)
    outer_state_store, outer_session_store = _install_outer_stores(settings)
    runtime = build_control_runtime(settings)
    try:
        mcp = build_mcp(
            runtime=runtime,
            own_runtime_lifespan=True,
        )
        _assert_outer_is_restored(outer_state_store, outer_session_store)

        async with mcp._mcp_server.lifespan(mcp._mcp_server):
            _assert_runtime_is_installed(runtime)

        _assert_outer_is_restored(outer_state_store, outer_session_store)
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_mcp_http_sessions_do_not_own_process_runtime(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="mcp",
        auth_mode="none",
        remote_enabled=False,
    )
    configure_settings(settings)
    outer_state_store, outer_session_store = _install_outer_stores(settings)
    runtime = build_control_runtime(settings)
    try:
        mcp = build_mcp(runtime=runtime)

        async with mcp._mcp_server.lifespan(mcp._mcp_server):
            _assert_outer_is_restored(outer_state_store, outer_session_store)
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


def test_mcp_http_host_owns_control_runtime_once(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="mcp",
        auth_mode="none",
        remote_enabled=False,
    )
    configure_settings(settings)
    outer_state_store, outer_session_store = _install_outer_stores(settings)
    runtime = build_control_runtime(settings)
    try:
        mcp = build_mcp(runtime=runtime)
        app = build_mcp_http_app(mcp, runtime=runtime)
        _assert_outer_is_restored(outer_state_store, outer_session_store)

        with TestClient(app) as client:
            _assert_runtime_is_installed(runtime)
            assert client.get("/healthz").status_code == 200

        _assert_outer_is_restored(outer_state_store, outer_session_store)
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


def test_mcp_http_inner_startup_failure_closes_control_runtime(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
        mode="mcp",
        auth_mode="none",
        remote_enabled=False,
    )
    configure_settings(settings)
    outer_state_store, outer_session_store = _install_outer_stores(settings)
    runtime = build_control_runtime(settings)

    @asynccontextmanager
    async def failing_sdk_lifespan(
        _app: Starlette,
    ) -> AsyncGenerator[None]:
        _assert_runtime_is_installed(runtime)
        raise RuntimeError("sdk startup failed")
        yield

    inner = Starlette(lifespan=failing_sdk_lifespan)
    app, _public_routes = mcp_app._add_public_routes_to_mcp_http_app(
        inner,
        runtime=runtime,
    )
    try:
        with (
            pytest.raises(RuntimeError, match="sdk startup failed"),
            TestClient(app),
        ):
            pytest.fail("inner startup failure must prevent serving")

        _assert_outer_is_restored(outer_state_store, outer_session_store)
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)
