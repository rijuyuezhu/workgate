import asyncio

import pytest

from tests.helpers import get_test_tool_session_store as get_tool_session_store
from workgate.config.role_config import (
    get_role_config,
    resolve_shared_role_config,
)
from workgate.config.settings import Settings, get_settings
from workgate.jobs import managed as jobs_managed
from workgate.jobs.managed import ManagedJobsRuntime, use_managed_jobs_runtime
from workgate.protocol.ids import new_session_id


def test_managed_jobs_handler_registration_is_owner_scoped() -> None:
    first = ManagedJobsRuntime()
    second = ManagedJobsRuntime()

    async def first_handler(_context, payload):
        return payload

    async def second_handler(_context, payload):
        return payload

    with pytest.raises(ValueError, match="must not be empty"):
        first.register_handler("   ", first_handler)
    first.register_handler("copy", first_handler)
    first.register_handler("copy", first_handler)
    second.register_handler("copy", second_handler)

    assert first.handlers == {"copy": first_handler}
    assert second.handlers == {"copy": second_handler}
    with pytest.raises(ValueError, match="already registered"):
        first.register_handler("copy", second_handler)


@pytest.mark.asyncio
async def test_managed_jobs_runtime_close_drains_tasks_and_stops_admission(
    managed_jobs_runtime_owner: ManagedJobsRuntime,
) -> None:
    runtime = managed_jobs_runtime_owner
    get_settings().workspace_root.mkdir(parents=True, exist_ok=True)
    store = get_tool_session_store()
    store.clear()
    session_id = store.create_session(
        session_id=str(new_session_id()), workdir="."
    ).session_id
    entered = asyncio.Event()

    async def handler(context, _payload):
        await context.update_progress(phase="running")
        entered.set()
        await asyncio.Event().wait()

    runtime.register_handler("test-runtime-close", handler)
    started = await jobs_managed.start_managed_job(
        session_id,
        "test-runtime-close",
        {},
    )
    await entered.wait()

    assert started.job_id in runtime.tasks
    assert started.job_id in runtime.leases

    await runtime.aclose()

    assert runtime.tasks == {}
    assert runtime.leases == {}
    listed = await jobs_managed.managed_job_list_execute(session_id, True)
    row = next(job for job in listed.jobs if job.job_id == started.job_id)
    assert row.status == "stopped"
    assert row.completed_at is not None

    await runtime.aclose()
    with pytest.raises(RuntimeError, match="not accepting new work"):
        await jobs_managed.start_managed_job(
            session_id, "test-runtime-close", {}
        )
    with pytest.raises(RuntimeError, match="not accepting new work"):
        await jobs_managed._retry_managed_job(session_id, started.job_id)
    with pytest.raises(RuntimeError, match="closed"):
        runtime.register_handler("late", handler)
    with pytest.raises(RuntimeError, match="cannot be restarted after close"):
        await runtime.start()


@pytest.mark.asyncio
async def test_managed_jobs_runtime_rejects_a_second_event_loop() -> None:
    runtime = ManagedJobsRuntime()
    foreign_loop = asyncio.new_event_loop()
    runtime._loop = foreign_loop
    try:
        with pytest.raises(RuntimeError, match="cannot span event loops"):
            await runtime.start()
        with pytest.raises(RuntimeError, match="owning event loop"):
            await runtime.aclose()
    finally:
        foreign_loop.close()


@pytest.mark.asyncio
async def test_managed_jobs_context_propagates_owned_role_config_to_tasks(
    tmp_path,
) -> None:
    role_config = resolve_shared_role_config(
        Settings(state_dir=tmp_path / "state", max_job_log_bytes=321)
    )
    runtime = ManagedJobsRuntime(role_config=role_config)

    async def inherited_role_config():
        await asyncio.sleep(0)
        return get_role_config()

    with use_managed_jobs_runtime(runtime):
        inherited = await asyncio.create_task(inherited_role_config())

    assert inherited is role_config
