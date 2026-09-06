import asyncio

import pytest

import workgate.composition.services as composition_services
import workgate.terminal.bridge as terminal_bridge
import workgate.terminal.conpty as terminal_conpty
from workgate.composition.services import (
    build_runtime_services,
    install_runtime_services,
)
from workgate.config.settings import Settings
from workgate.control.runtime import build_control_runtime
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
from workgate.ops.utils.session_copy import SESSION_COPY_MANAGED_KIND
from workgate.persistence import (
    FileStateStore,
    configure_state_store,
    get_state_store,
)
from workgate.remote.manager import (
    RemoteManager,
    configure_remote_manager,
    remote_manager,
)
from workgate.terminal.runtime import build_terminal_runtime
from workgate.tool_session import (
    configure_tool_session_store,
    get_tool_session_store,
)
from workgate.tool_session.store import ToolSessionStore
from workgate.ui.http.live_state import (
    build_human_ui_runtime,
    human_ui_runtime,
)


def test_runtime_services_install_explicit_store_dependencies(tmp_path):
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
        assert services.tool_session_store._settings() is settings
    finally:
        installation.close()


def test_runtime_service_construction_does_not_install_compatibility_globals(
    tmp_path,
):
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
    )
    try:
        services = build_runtime_services(settings)

        assert services.state_store is not outer_state_store
        assert services.tool_session_store is not outer_session_store
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_control_runtime_lifespan_restores_outer_compatibility_bindings(
    tmp_path,
):
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-state",
        )
    )
    try:
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store

        async with runtime.lifespan() as active:
            assert active is runtime
            assert get_state_store() is runtime.services.state_store
            assert (
                get_tool_session_store() is runtime.services.tool_session_store
            )

        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
        await runtime.aclose()
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            await runtime.start()
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_worker_runtime_lifespan_restores_bindings_after_exception(
    tmp_path,
):
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    runtime = build_executor_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "worker-state",
        )
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
        await runtime.aclose()
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


def test_runtime_service_installation_rolls_back_partial_startup(
    tmp_path, monkeypatch
):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "runtime-state",
    )
    outer_state_store = FileStateStore(lambda: tmp_path / "outer-state")
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    services = build_runtime_services(settings)

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
        monkeypatch.undo()
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_controller_runtime_startup_failure_closes_started_bindings(
    tmp_path, monkeypatch
):
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-state",
        )
    )
    original_start = runtime.start

    async def fail_after_start() -> None:
        await original_start()
        raise RuntimeError("startup failed")

    monkeypatch.setattr(runtime, "start", fail_after_start)
    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with runtime.lifespan():
                pytest.fail("startup failure must prevent entering the body")

        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_controller_runtime_remote_binding_failure_closes_remote_manager(
    tmp_path, monkeypatch
):
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-state",
        )
    )

    def fail_remote_binding(_manager):
        raise RuntimeError("remote binding failed")

    monkeypatch.setattr(
        "workgate.control.runtime.configure_remote_manager",
        fail_remote_binding,
    )
    try:
        with pytest.raises(RuntimeError, match="remote binding failed"):
            await runtime.start()

        assert runtime.remote_manager._closed is True
        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_kind", ["controller", "worker"])
async def test_terminal_start_failure_restores_store_bindings(
    tmp_path, monkeypatch, runtime_kind
):
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / f"{runtime_kind}-state",
        remote_enabled=False,
    )
    runtime = (
        build_control_runtime(settings)
        if runtime_kind == "controller"
        else build_executor_runtime(settings)
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
async def test_worker_runtime_cancellation_closes_bindings(tmp_path):
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    runtime = build_executor_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "worker-state",
        )
    )
    entered = asyncio.Event()

    async def run_until_cancelled() -> None:
        async with runtime.lifespan():
            entered.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(run_until_cancelled())
        await entered.wait()
        assert get_state_store() is runtime.services.state_store
        assert get_tool_session_store() is runtime.services.tool_session_store

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
    finally:
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_controller_runtime_owns_and_restores_remote_manager_binding(
    tmp_path,
):
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_manager = RemoteManager(
        lambda: outer_settings,
        state_store=outer_state_store,
    )
    configure_remote_manager(outer_manager)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-state",
        )
    )
    try:
        assert remote_manager() is outer_manager
        async with runtime.lifespan():
            assert remote_manager() is runtime.remote_manager
            assert runtime.remote_manager._loop is asyncio.get_running_loop()

        assert remote_manager() is outer_manager
        assert runtime.remote_manager._closed is True
    finally:
        configure_remote_manager(None)


@pytest.mark.asyncio
async def test_controller_runtime_owns_and_restores_ui_and_oauth_bindings(
    tmp_path,
) -> None:
    outer = build_human_ui_runtime()
    outer_oauth = OAuthState(tmp_path / "outer-oauth-state")
    previous_oauth = configure_oauth_state(outer_oauth)
    await outer.start()
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-state",
            remote_enabled=False,
        )
    )
    try:
        assert human_ui_runtime() is outer
        assert oauth_state() is outer_oauth
        async with runtime.lifespan():
            assert human_ui_runtime() is runtime.human_ui_runtime
            assert oauth_state() is runtime.oauth_state
            assert (
                runtime.human_ui_runtime.terminal_connections._loop
                is asyncio.get_running_loop()
            )
            assert (
                runtime.human_ui_runtime.remote_files._loop
                is asyncio.get_running_loop()
            )

        assert human_ui_runtime() is outer
        assert oauth_state() is outer_oauth
    finally:
        await outer.aclose()
        configure_oauth_state(previous_oauth)


@pytest.mark.asyncio
async def test_controller_runtime_closes_ui_oauth_remote_and_terminal_in_order(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-state",
            remote_enabled=False,
        )
    )
    await runtime.start()
    events: list[str] = []
    jobs_close = runtime.managed_jobs_runtime.aclose
    ui_close = runtime.human_ui_runtime.aclose
    oauth_close = runtime.oauth_state.aclose
    remote_close = runtime.remote_manager.aclose
    terminal_close = runtime.terminal_runtime.aclose

    async def close_jobs() -> None:
        events.append("jobs")
        await jobs_close()

    async def close_ui() -> None:
        events.append("ui")
        await ui_close()

    async def close_oauth() -> None:
        events.append("oauth")
        await oauth_close()

    async def close_remote() -> None:
        events.append("remote")
        await remote_close()

    async def close_terminal() -> None:
        events.append("terminal")
        await terminal_close()

    monkeypatch.setattr(runtime.managed_jobs_runtime, "aclose", close_jobs)
    monkeypatch.setattr(runtime.human_ui_runtime, "aclose", close_ui)
    monkeypatch.setattr(runtime.oauth_state, "aclose", close_oauth)
    monkeypatch.setattr(runtime.remote_manager, "aclose", close_remote)
    monkeypatch.setattr(runtime.terminal_runtime, "aclose", close_terminal)

    await runtime.aclose()

    assert events == ["jobs", "ui", "oauth", "remote", "terminal"]


@pytest.mark.asyncio
async def test_human_ui_start_failure_rolls_back_controller_dependencies(
    tmp_path,
    monkeypatch,
) -> None:
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    outer_manager = RemoteManager(
        lambda: outer_settings,
        state_store=outer_state_store,
    )
    outer_terminal = build_terminal_runtime()
    outer_ui = build_human_ui_runtime()
    outer_oauth = OAuthState(tmp_path / "outer-oauth-state")
    previous_oauth = configure_oauth_state(outer_oauth)
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    configure_remote_manager(outer_manager)
    await outer_terminal.start()
    await outer_ui.start()
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-state",
            remote_enabled=False,
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
        assert remote_manager() is outer_manager
        assert oauth_state() is outer_oauth
        assert terminal_bridge._bridge_registry() is outer_terminal.bridges
        assert terminal_conpty._conpty_registry() is outer_terminal.conpty
        assert human_ui_runtime() is outer_ui
        assert runtime.oauth_state._closed is True
        assert runtime.remote_manager._closed is True
        assert runtime.terminal_runtime._closed is True
        assert runtime._closed is True
    finally:
        await runtime.aclose()
        await outer_ui.aclose()
        await outer_terminal.aclose()
        configure_oauth_state(previous_oauth)
        configure_remote_manager(None)
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
async def test_oauth_start_failure_rolls_back_controller_dependencies(
    tmp_path,
    monkeypatch,
) -> None:
    outer_settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    outer_state_store = FileStateStore(lambda: outer_settings.state_dir)
    outer_session_store = ToolSessionStore(
        state_store=outer_state_store,
        settings_provider=lambda: outer_settings,
    )
    outer_manager = RemoteManager(
        lambda: outer_settings,
        state_store=outer_state_store,
    )
    outer_terminal = build_terminal_runtime()
    outer_oauth = OAuthState(tmp_path / "outer-oauth-state")
    previous_oauth = configure_oauth_state(outer_oauth)
    configure_state_store(outer_state_store)
    configure_tool_session_store(outer_session_store)
    configure_remote_manager(outer_manager)
    await outer_terminal.start()
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-state",
            remote_enabled=False,
        )
    )

    def fail_oauth_start() -> int:
        raise RuntimeError("OAuth start failed")

    monkeypatch.setattr(runtime.oauth_state, "start", fail_oauth_start)
    try:
        with pytest.raises(RuntimeError, match="OAuth start failed"):
            await runtime.start()

        assert get_state_store() is outer_state_store
        assert get_tool_session_store() is outer_session_store
        assert remote_manager() is outer_manager
        assert oauth_state() is outer_oauth
        assert terminal_bridge._bridge_registry() is outer_terminal.bridges
        assert terminal_conpty._conpty_registry() is outer_terminal.conpty
        assert runtime.remote_manager._closed is True
        assert runtime.terminal_runtime._closed is True
        assert runtime._closed is True
    finally:
        await runtime.aclose()
        await outer_terminal.aclose()
        configure_oauth_state(previous_oauth)
        configure_remote_manager(None)
        configure_tool_session_store(None)
        configure_state_store(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_kind", ["controller", "worker"])
async def test_process_runtimes_own_and_restore_terminal_bindings(
    tmp_path,
    runtime_kind,
):
    outer = build_terminal_runtime()
    await outer.start()
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / f"{runtime_kind}-state",
        remote_enabled=False,
    )
    runtime = (
        build_control_runtime(settings)
        if runtime_kind == "controller"
        else build_executor_runtime(settings)
    )
    try:
        assert terminal_bridge._bridge_registry() is outer.bridges
        assert terminal_conpty._conpty_registry() is outer.conpty

        async with runtime.lifespan():
            assert (
                terminal_bridge._bridge_registry()
                is runtime.terminal_runtime.bridges
            )
            assert (
                terminal_conpty._conpty_registry()
                is runtime.terminal_runtime.conpty
            )
            assert (
                runtime.terminal_runtime.bridges._loop
                is asyncio.get_running_loop()
            )
            assert (
                runtime.terminal_runtime.conpty._loop
                is asyncio.get_running_loop()
            )

        assert terminal_bridge._bridge_registry() is outer.bridges
        assert terminal_conpty._conpty_registry() is outer.conpty
    finally:
        await outer.aclose()


@pytest.mark.asyncio
async def test_controller_runtime_owns_and_restores_managed_jobs_binding(
    tmp_path,
) -> None:
    outer = ManagedJobsRuntime()
    await outer.start()
    previous = configure_managed_jobs_runtime(outer)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-jobs-state",
            remote_enabled=False,
        )
    )
    try:
        assert managed_jobs_runtime() is outer
        assert (
            SESSION_COPY_MANAGED_KIND in runtime.managed_jobs_runtime.handlers
        )
        async with runtime.lifespan():
            assert managed_jobs_runtime() is runtime.managed_jobs_runtime
            assert (
                runtime.managed_jobs_runtime._loop is asyncio.get_running_loop()
            )

        assert managed_jobs_runtime() is outer
        assert runtime.managed_jobs_runtime._closed is True
    finally:
        await outer.aclose()
        configure_managed_jobs_runtime(previous)


@pytest.mark.asyncio
async def test_controller_runtime_managed_jobs_binding_failure_closes_owner(
    tmp_path, monkeypatch
) -> None:
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path,
            state_dir=tmp_path / "controller-jobs-failure-state",
            remote_enabled=False,
        )
    )

    def fail_managed_jobs_binding(_runtime):
        raise RuntimeError("managed jobs binding failed")

    monkeypatch.setattr(
        "workgate.control.runtime.configure_managed_jobs_runtime",
        fail_managed_jobs_binding,
    )

    with pytest.raises(RuntimeError, match="managed jobs binding failed"):
        await runtime.start()

    assert runtime.managed_jobs_runtime._closed is True
    assert runtime.managed_jobs_runtime.tasks == {}
    assert runtime.managed_jobs_runtime.leases == {}
