import pytest

from workgate.config.control import resolve_control_config
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.control.runtime import build_control_runtime
from workgate.control.services import build_control_services
from workgate.control.session_copy import SESSION_COPY_MANAGED_KIND
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.services import build_runtime_services
from workgate.jobs.managed import (
    ManagedJobsRuntime,
    managed_jobs_runtime,
    use_managed_jobs_runtime,
)
from workgate.oauth.core.state import OAuthState, oauth_state, use_oauth_state
from workgate.persistence import (
    FileStateStore,
    get_state_store,
    use_state_store,
)


def _outer_services(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / "outer-state",
    )
    return settings, FileStateStore(lambda: settings.state_dir)


def test_executor_runtime_services_construct_explicit_store_dependencies(
    tmp_path,
):
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
    )
    services = build_runtime_services(resolve_executor_config(settings))

    assert services.state_store.layout.root == settings.state_dir
    with use_state_store(services.state_store):
        assert get_state_store() is services.state_store


def test_control_services_do_not_construct_machine_session_authority(tmp_path):
    settings = Settings(
        workspace_root=tmp_path / "executor-workspace-must-not-be-used",
        state_dir=tmp_path / "control-state",
    )
    services = build_control_services(resolve_control_config(settings))

    assert services.state_store.layout.root == settings.state_dir
    assert not hasattr(services, "tool_session_store")


def test_role_service_construction_does_not_rebind_state_context(tmp_path):
    outer_settings, outer_state_store = _outer_services(tmp_path)
    with use_state_store(outer_state_store):
        control = build_control_services(
            resolve_control_config(
                Settings(
                    workspace_root=tmp_path / "unused-control-workspace",
                    state_dir=tmp_path / "control-state",
                )
            )
        )
        executor = build_runtime_services(
            resolve_executor_config(
                Settings(
                    workspace_root=tmp_path,
                    state_dir=tmp_path / "executor-state",
                )
            )
        )

        assert control.state_store is not outer_state_store
        assert executor.state_store is not outer_state_store
        assert executor.tool_session_store is not None
        assert get_state_store() is outer_state_store
        assert outer_settings.state_dir == outer_state_store.layout.root


@pytest.mark.asyncio
async def test_control_runtime_lifespan_does_not_rebind_state_context(tmp_path):
    _, outer_state_store = _outer_services(tmp_path)
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path / "control-must-not-use-workspace",
            state_dir=tmp_path / "control-state",
            auth_mode="none",
        )
    )
    with use_state_store(outer_state_store):
        async with runtime.lifespan() as active:
            assert active is runtime
            assert get_state_store() is outer_state_store
        assert get_state_store() is outer_state_store
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await runtime.start()


@pytest.mark.asyncio
async def test_executor_runtime_lifespan_does_not_rebind_state_context(
    tmp_path,
):
    _, outer_state_store = _outer_services(tmp_path)
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                workspace_root=tmp_path,
                state_dir=tmp_path / "executor-state",
            )
        ),
        enable_control_connection=False,
    )
    with use_state_store(outer_state_store):
        with pytest.raises(RuntimeError, match="boom"):
            async with runtime.lifespan():
                assert get_state_store() is outer_state_store
                raise RuntimeError("boom")
        assert get_state_store() is outer_state_store


@pytest.mark.asyncio
async def test_executor_terminal_start_failure_preserves_state_context(
    tmp_path, monkeypatch
):
    _, outer_state_store = _outer_services(tmp_path)
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                workspace_root=tmp_path,
                state_dir=tmp_path / "executor-state",
            )
        ),
        enable_control_connection=False,
    )

    async def fail_terminal_start() -> None:
        raise RuntimeError("terminal start failed")

    monkeypatch.setattr(runtime.terminal_runtime, "start", fail_terminal_start)
    with use_state_store(outer_state_store):
        with pytest.raises(RuntimeError, match="terminal start failed"):
            await runtime.start()
        assert get_state_store() is outer_state_store
        assert runtime._closed is True
    await runtime.aclose()


@pytest.mark.asyncio
async def test_control_runtime_does_not_rebind_oauth_or_managed_jobs_context(
    tmp_path,
) -> None:
    outer_oauth = OAuthState(tmp_path / "outer-oauth-state")
    outer_jobs = ManagedJobsRuntime()
    await outer_jobs.start()
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path / "unused-control-workspace",
            state_dir=tmp_path / "control-state",
            auth_mode="none",
        )
    )
    assert runtime.managed_jobs_runtime.role_config is runtime.config
    try:
        with use_oauth_state(outer_oauth), use_managed_jobs_runtime(outer_jobs):
            assert oauth_state() is outer_oauth
            assert managed_jobs_runtime() is outer_jobs
            async with runtime.lifespan():
                assert oauth_state() is outer_oauth
                assert managed_jobs_runtime() is outer_jobs
                assert (
                    SESSION_COPY_MANAGED_KIND
                    in runtime.managed_jobs_runtime.handlers
                )
            assert oauth_state() is outer_oauth
            assert managed_jobs_runtime() is outer_jobs
    finally:
        await runtime.aclose()
        await outer_jobs.aclose()


@pytest.mark.asyncio
async def test_control_human_ui_start_failure_rolls_back_control_dependencies(
    tmp_path, monkeypatch
) -> None:
    _, outer_state_store = _outer_services(tmp_path)
    outer_oauth = OAuthState(tmp_path / "outer-oauth-state")
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
        with use_state_store(outer_state_store), use_oauth_state(outer_oauth):
            with pytest.raises(RuntimeError, match="Human UI start failed"):
                await runtime.start()
            assert get_state_store() is outer_state_store
            assert oauth_state() is outer_oauth
        assert runtime.oauth_state._closed is True
        assert runtime._closed is True
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_control_runtime_close_can_retry_failed_owned_cleanup(
    tmp_path,
) -> None:
    runtime = build_control_runtime(
        Settings(
            workspace_root=tmp_path / "unused-control-workspace",
            state_dir=tmp_path / "control-state",
            auth_mode="none",
        )
    )
    await runtime.start()
    original_close = runtime.managed_jobs_runtime.aclose
    calls = 0

    async def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("close failed once")
        await original_close()

    runtime.managed_jobs_runtime.aclose = fail_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="close failed once"):
        await runtime.aclose()
    await runtime.aclose()
    assert calls == 2
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await runtime.start()


@pytest.mark.asyncio
async def test_executor_runtime_close_can_retry_failed_owned_cleanup(
    tmp_path,
) -> None:
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                workspace_root=tmp_path,
                state_dir=tmp_path / "executor-state",
            )
        ),
        enable_control_connection=False,
    )
    await runtime.start()
    original_close = runtime.terminal_runtime.aclose
    calls = 0

    async def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("close failed once")
        await original_close()

    runtime.terminal_runtime.aclose = fail_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="close failed once"):
        await runtime.aclose()
    await runtime.aclose()
    assert calls == 2
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await runtime.start()
