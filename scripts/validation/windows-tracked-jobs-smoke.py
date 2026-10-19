"""Native Windows tracked shell jobs: completion, retry, ownership and restart."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Native Windows Python is required")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.config.executor import resolve_executor_config  # noqa: E402
from workgate.config.settings import clear_settings_cache, get_settings  # noqa: E402
from workgate.executor.jobs import ExecutorJobService  # noqa: E402
from workgate.executor.services import build_runtime_services  # noqa: E402
from workgate.executor.terminal.runtime import (  # noqa: E402
    build_terminal_runtime,
    use_terminal_runtime,
)
from workgate.persistence import use_state_store  # noqa: E402
from workgate.protocol.ids import new_session_id  # noqa: E402


async def finished(jobs: ExecutorJobService, session_id: str, job_id: str):
    for _ in range(150):
        result = await jobs.tail(session_id, job_id)
        if result.job.status not in {
            "starting",
            "running",
            "retrying",
            "stopping",
        }:
            return result
        await asyncio.sleep(0.1)
    raise AssertionError(f"job did not finish: {job_id}")


async def shell_retired(runtime) -> None:
    # Retry must not overlap a still-live previous attempt, even if its
    # completion status arrived before PowerShell finished exiting.
    for _ in range(100):
        if not await asyncio.to_thread(runtime.conpty.active):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("completed job left a live ConPTY shell")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-windows-jobs-") as temp:
        root = Path(temp)
        os.environ["WORKGATE_STATE_DIR"] = str(root / "state")
        os.environ["WORKGATE_DEFAULT_WORKDIR"] = str(root)
        clear_settings_cache()
        config = resolve_executor_config(get_settings())
        services = build_runtime_services(config)
        store = services.tool_session_store
        jobs = ExecutorJobService(config, store)
        runtime = build_terminal_runtime()
        await runtime.start()
        owner = str(new_session_id())
        other = str(new_session_id())
        with (
            use_state_store(services.state_store),
            use_terminal_runtime(runtime),
        ):
            store.create_session(session_id=owner, workdir=str(root))
            store.create_session(session_id=other, workdir=str(root))
            try:
                failed = await jobs.start(
                    owner,
                    "Write-Output 'WIN_JOB_FAILED'; exit 7",
                    name="failure",
                )
                result = await finished(jobs, owner, failed.job_id)
                assert result.job.status == "failed", result
                assert result.job.exit_code == 7, result
                assert "WIN_JOB_FAILED" in result.output, result

                await shell_retired(runtime)
                retried = await jobs.retry(owner, failed.job_id)
                assert retried.attempts == 2, retried
                result = await finished(jobs, owner, failed.job_id)
                assert (
                    result.job.status == "failed" and result.job.exit_code == 7
                )
                assert result.job.attempts == 2, result

                success = await jobs.start(
                    owner,
                    "Write-Output 'WIN_JOB_SUCCESS'; exit 0",
                    name="success",
                )
                result = await finished(jobs, owner, success.job_id)
                assert (
                    result.job.status == "succeeded"
                    and result.job.exit_code == 0
                )
                assert "WIN_JOB_SUCCESS" in result.output, result

                listed = await jobs.list(owner)
                assert {success.job_id, failed.job_id}.issubset(
                    {j.job_id for j in listed.jobs}
                ), listed
                try:
                    await jobs.tail(other, success.job_id)
                except KeyError:
                    pass
                else:
                    raise AssertionError(
                        "Cross-session job lookup was permitted"
                    )

                sleeping = await jobs.start(
                    owner, "Start-Sleep -Seconds 45", name="stop"
                )
                stopped = await jobs.stop(owner, sleeping.job_id)
                assert stopped.killed, stopped
                status = await finished(jobs, owner, sleeping.job_id)
                assert status.job.status == "stopped", status

                interrupted = await jobs.start(
                    owner, "Start-Sleep -Seconds 45", name="restart"
                )
                assert interrupted.job_id
            finally:
                await runtime.aclose()

        restarted = build_terminal_runtime()
        await restarted.start()
        try:
            with (
                use_state_store(services.state_store),
                use_terminal_runtime(restarted),
            ):
                assert await jobs.reconcile()
                entry = await jobs.tail(owner, interrupted.job_id)
                assert entry.job.status != "running", entry
                assert entry.job.status != "succeeded", entry
        finally:
            await restarted.aclose()
    print(
        "Native Windows jobs start/list/tail/stop/retry/ownership/restart: PASS"
    )


if __name__ == "__main__":
    asyncio.run(main())
