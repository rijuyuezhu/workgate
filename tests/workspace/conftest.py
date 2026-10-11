import pytest


def _reset_managed_deferred_sequence(job_recovery) -> None:
    with job_recovery._MANAGED_DEFERRED_SEQUENCE_LOCK:
        job_recovery._MANAGED_DEFERRED_NEXT_SEQUENCE = None


@pytest.fixture
async def managed_jobs_runtime_owner():
    from workgate.jobs import recovery as job_recovery
    from workgate.jobs.managed import (
        ManagedJobsRuntime,
        use_managed_jobs_runtime,
    )

    runtime = ManagedJobsRuntime()
    await runtime.start()
    _reset_managed_deferred_sequence(job_recovery)
    try:
        with use_managed_jobs_runtime(runtime):
            yield runtime
    finally:
        await runtime.aclose()
        _reset_managed_deferred_sequence(job_recovery)
