import pytest

import workgate.composition.services as composition_services
from workgate.composition.services import (
    build_control_services,
    build_runtime_services,
    install_control_services,
    install_runtime_services,
)
from workgate.config.settings import Settings
from workgate.control.runtime import build_control_runtime
from workgate.control.session_copy import SESSION_COPY_MANAGED_KIND
from workgate.executor.runtime import build_executor_runtime
from workgate.jobs.managed import (
    ManagedJobsRuntime,
    configure_managed_jobs_runtime,
    managed_jobs_runtime,
)
from workgate.oauth.core.state import (
    OAuthState,
    configure_oauth_state,
    oauth_state,
)
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
from workgate.ui.http.live_state import (
    build_human_ui_runtime,
    human_ui_runtime,
)


def _outer_services(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    state_store = FileStateStore(lambda: settings.state_dir)
    session_store = ToolSessionStore(
        state_store=state_store,
        settings_provider=lambda: settings,
    )
    return settings, state_store, session_store


def test_executor_runtime_services_install_explicit_store_dependencies(
    tmp_path,
):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
    )
    services = build_runtime_services(settings)

    installation = install_runtime_services(services)
    try:
        assert get_state_store() is services.state_store
        assert get_tool_session_store() is services.tool_session_store
        assert services.state_store.layout.root == settings.state_dir
    finally:
        installation.close()
        configure_tool_session_store(None)
        configure_state_store(None)


def test_control_services_do_not_construct_machine_session_authority(tmp_path):
    settings = Settings(
        workspace_root=tmp_path / "executor-workspace-must-not-be-used",
        state_dir=tmp_path / "control-state",
    )
    services = build_control_services(settings)

    assert services.state_store.layout.root == settings.state_dir
    assert not hasattr(services, "tool_session_store")


def test_role_service_construction_does_not_install_globals(tmp_path):
    outer_settings, outer_state_store, outer_session_store = _outer_services(
        tmp_path
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    try:
        control = build_control_services(
            Settings(
                workspace_root=tmp_path / "unused-control-workspace",
                state_dir=tmp_path / "control-state",
            )
        )
        executor = build_runtime_services(
            Settings(
                workspace_root=tmp_path,
                state_dir=tmp_path / "executor-state",
            )
        )

        assert control.state_store is not outer_state_store
        assert executor.state_store is not outer_state_store
        assert executor.tool_session_store is not outer_session_store
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
        assert outer_settings.state_dir == outer_state_store.layout.root
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


def test_control_service_installation_never_rebinds_tool_sessions(tmp_path):
    _, outer_state_store, outer_session_store = _outer_services(tmp_path)
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    services = build_control_services(
        Settings(
            workspace_root=tmp_path / "unused",
            state_dir=tmp_path / "control-state",
        )
    )

    installation = install_control_services(services)
    try:
        assert get_state_store() is services.state_store
        assert get_tool_session_store() is outer_session_store
    finally:
        installation.close()
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_control_runtime_lifespan_restores_state_without_session_binding(
    tmp_path,
):
    _, outer_state_store, outer_session_store = _outer_services(tmp_path)
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path / "control-must-not-use-workspace",
            state_dir=tmp_path / "control-state",
            auth_mode="none",
        )
    )
    try:
        assert not hasattr(runtime.services, "tool_session_store")
        async with runtime.lifespan() as active:
            assert active is runtime
            assert get_state_store() is runtime.services.state_store
            assert get_tool_session_store() is outer_session_store

        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            await runtime.start()
    finally:
        await runtime.aclose()
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_executor_runtime_lifespan_restores_bindings_after_exception(
    tmp_path,
):
    _, outer_state_store, outer_session_store = _outer_services(tmp_path)
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    runtime = build_executor_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "executor-state",
            remote_enabled=False,
        ),
        enable_control_connection=False,
    )
    try:
        with pytest.raises(RuntimeError, match="boom"):
            async with runtime.lifespan():
                assert get_state_store() is runtime.services.state_store
                assert (
                    get_tool_session_store()
                    is runtime.services.tool_session_store
                )
                raise RuntimeError("boom")

        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
    finally:
        await runtime.aclose()
        configure_tool_session_store(None)
        configure_state_store(None)


def test_executor_service_installation_rolls_back_partial_startup(
    tmp_path, monkeypatch
):
    _, outer_state_store, outer_session_store = _outer_services(tmp_path)
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    services = build_runtime_services(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "executor-state",
        )
    )

    def fail_session_install(_store):
        raise RuntimeError("session install failed")

    monkeypatch.setattr(
        composition_services,
        "configure_tool_session_store",
        fail_session_install,
    )
    try:
        with pytest.raises(RuntimeError, match="session install failed"):
            install_runtime_services(services)
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_executor_terminal_start_failure_restores_store_bindings(
    tmp_path, monkeypatch
):
    _, outer_state_store, outer_session_store = _outer_services(tmp_path)
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    runtime = build_executor_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "executor-state",
            remote_enabled=False,
        ),
        enable_control_connection=False,
    )

    async def fail_terminal_start() -> None:
        raise RuntimeError("terminal start failed")

    monkeypatch.setattr(runtime.terminal_runtime, "start", fail_terminal_start)
    try:
        with pytest.raises(RuntimeError, match="terminal start failed"):
            await runtime.start()
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
        assert runtime._closed is True
    finally:
        await runtime.aclose()
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_control_runtime_owns_and_restores_ui_oauth_and_managed_jobs(
    tmp_path,
) -> None:
    outer_ui = build_human_ui_runtime()
    await outer_ui.start()
    outer_oauth = OAuthState(tmp_path / "outer-oauth-state")
    previous_oauth = configure_oauth_state(outer_oauth)
    outer_jobs = ManagedJobsRuntime()
    await outer_jobs.start()
    previous_jobs = configure_managed_jobs_runtime(outer_jobs)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path / "unused-control-workspace",
            state_dir=tmp_path / "control-state",
            auth_mode="none",
        )
    )
    try:
        assert human_ui_runtime() is outer_ui
        assert oauth_state() is outer_oauth
        assert managed_jobs_runtime() is outer_jobs
        async with runtime.lifespan():
            assert human_ui_runtime() is runtime.human_ui_runtime
            assert oauth_state() is runtime.oauth_state
            assert managed_jobs_runtime() is runtime.managed_jobs_runtime
            assert (
                SESSION_COPY_MANAGED_KIND
                in runtime.managed_jobs_runtime.handlers
            )

        assert human_ui_runtime() is outer_ui
        assert oauth_state() is outer_oauth
        assert managed_jobs_runtime() is outer_jobs
    finally:
        await runtime.aclose()
        await outer_ui.aclose()
        await outer_jobs.aclose()
        configure_oauth_state(previous_oauth)
        configure_managed_jobs_runtime(previous_jobs)


@pytest.mark.asyncio
async def test_control_human_ui_start_failure_rolls_back_control_dependencies(
    tmp_path, monkeypatch
) -> None:
    _, outer_state_store, outer_session_store = _outer_services(tmp_path)
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    outer_oauth = OAuthState(tmp_path / "outer-oauth-state")
    previous_oauth = configure_oauth_state(outer_oauth)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path / "unused-control-workspace",
            state_dir=tmp_path / "control-state",
            auth_mode="none",
        )
    )

    async def fail_ui_start() -> None:
        raise RuntimeError("Human UI start failed")

    monkeypatch.setattr(runtime.human_ui_runtime, "start", fail_ui_start)
    try:
        with pytest.raises(RuntimeError, match="Human UI start failed"):
            await runtime.start()
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
        assert oauth_state() is outer_oauth
        assert runtime.oauth_state._closed is True
        assert runtime._closed is True
    finally:
        await runtime.aclose()
        configure_oauth_state(previous_oauth)
        configure_tool_session_store(None)
        configure_state_store(None)
