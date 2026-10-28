"""A source upgrade must not cut over active or uncertain Executor resources."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.errors import ExecutorResourceInventoryUnavailable
from workgate.executor.runtime import (
    SourceCutoverDeferred,
    build_executor_runtime,
)
from workgate.protocol.executor import (
    JobInventorySummary,
    SessionInventorySummary,
    ShellInventorySummary,
)

SESSION = "sess_0000000000000000000001"


@pytest.fixture
def idle_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=workspace, state_dir=tmp_path / "state")
        )
    )
    connection = SimpleNamespace(active_command_count=0)
    monkeypatch.setattr(runtime, "connection", connection)
    monkeypatch.setattr(runtime, "_started", True)
    metadata = SessionInventorySummary(
        session_id=SESSION, workdir=str(workspace)
    )
    inventory = SimpleNamespace(sessions=(metadata,), shells=(), jobs=())

    async def snapshot():
        return inventory

    monkeypatch.setattr(runtime, "_build_reconnect_hello", snapshot)

    async def all_shells(*_args):
        return set()

    monkeypatch.setattr(
        "workgate.executor.shell.authoritative_persistent_shell_ids_execute",
        all_shells,
    )
    return runtime, connection, inventory


@pytest.mark.asyncio
async def test_metadata_only_session_does_not_block_safe_cutover(
    idle_runtime,
) -> None:
    runtime, _connection, _inventory = idle_runtime
    await runtime.require_source_cutover_idle()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "session_flags", "shells", "jobs"),
    [
        (
            "persistent shell",
            {},
            (ShellInventorySummary(shell_id="shell-1", session_id=SESSION),),
            (),
        ),
        (
            "stale persistent shell flag",
            {"has_persistent_shells": True},
            (),
            (),
        ),
        ("active job flag", {"has_active_jobs": True}, (), ()),
        (
            "running job",
            {},
            (),
            (
                JobInventorySummary(
                    job_id="job-1", session_id=SESSION, status="running"
                ),
            ),
        ),
        (
            "uncertain lost job",
            {},
            (),
            (
                JobInventorySummary(
                    job_id="job-1", session_id=SESSION, status="lost"
                ),
            ),
        ),
    ],
)
async def test_live_or_uncertain_owned_resources_block(
    idle_runtime, reason, session_flags, shells, jobs
) -> None:
    runtime, _connection, inventory = idle_runtime
    inventory.sessions = tuple(
        row.model_copy(update=session_flags) for row in inventory.sessions
    )
    inventory.shells = shells
    inventory.jobs = jobs
    with pytest.raises(RuntimeError, match="persistent shells or jobs"):
        await runtime.require_source_cutover_idle()


def test_local_lost_job_evidence_is_never_sent_to_control() -> None:
    job = JobInventorySummary(
        job_id="job-old", session_id=SESSION, status="lost"
    )
    job._shell_absence_confirmed = True
    assert "shell_absence_confirmed" not in job.model_dump()
    assert "shell_absence_confirmed" not in job.model_dump_json()
    assert "shell_absence_confirmed" not in job.model_json_schema()
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JobInventorySummary.model_validate(
            {
                "job_id": "job-old",
                "session_id": SESSION,
                "status": "lost",
                "shell_absence_confirmed": True,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed", "exited", "stopped"])
async def test_confirmed_finished_job_history_does_not_block(
    idle_runtime, status: str
) -> None:
    runtime, _connection, inventory = idle_runtime
    inventory.jobs = (
        JobInventorySummary(job_id="job-1", session_id=SESSION, status=status),
    )
    await runtime.require_source_cutover_idle()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["command", "terminal"])
async def test_active_command_or_terminal_blocks_without_inventory(
    idle_runtime, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    runtime, connection, _inventory = idle_runtime

    async def forbidden():
        raise AssertionError("inventory should not run with active work")

    monkeypatch.setattr(runtime, "_build_reconnect_hello", forbidden)
    task = None
    if field == "command":
        connection.active_command_count = 1
    else:
        task = asyncio.create_task(asyncio.Event().wait())
        runtime._terminal_stream_tasks.add(task)
    try:
        with pytest.raises(RuntimeError, match="commands or terminal streams"):
            await runtime.require_source_cutover_idle()
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            runtime._terminal_stream_tasks.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_sessionless_shell_or_uncertain_shell_inventory_blocks(
    idle_runtime, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    runtime, _connection, _inventory = idle_runtime

    async def all_shells(*_args):
        return {"unowned-ui-shell"} if active else None

    monkeypatch.setattr(
        "workgate.executor.shell.authoritative_persistent_shell_ids_execute",
        all_shells,
    )
    with pytest.raises(
        (RuntimeError, ExecutorResourceInventoryUnavailable),
        match="persistent shell",
    ):
        await runtime.require_source_cutover_idle()


@pytest.mark.asyncio
async def test_uncertain_snapshot_fails_closed(
    idle_runtime, monkeypatch: pytest.MonkeyPatch
):
    runtime, _connection, _inventory = idle_runtime

    async def uncertain():
        raise ExecutorResourceInventoryUnavailable("unreconciled job state")

    monkeypatch.setattr(runtime, "_build_reconnect_hello", uncertain)
    with pytest.raises(ExecutorResourceInventoryUnavailable):
        await runtime.require_source_cutover_idle()


@pytest.mark.asyncio
async def test_new_command_during_inventory_fails_closed(
    idle_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, connection, inventory = idle_runtime

    async def raced():
        connection.active_command_count = 1
        return inventory

    monkeypatch.setattr(runtime, "_build_reconnect_hello", raced)
    with pytest.raises(RuntimeError, match="commands or terminal streams"):
        await runtime.require_source_cutover_idle()


@pytest.mark.asyncio
async def test_not_started_or_disconnected_fails_closed(idle_runtime) -> None:
    runtime, _connection, _inventory = idle_runtime
    runtime._started = False
    with pytest.raises(RuntimeError, match="running Executor connection"):
        await runtime.require_source_cutover_idle()
    runtime._started = True
    runtime.connection = None
    with pytest.raises(RuntimeError, match="running Executor connection"):
        await runtime.require_source_cutover_idle()


@pytest.mark.asyncio
async def test_readiness_reuses_the_authoritative_reconnect_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=workspace, state_dir=tmp_path / "state")
        )
    )
    monkeypatch.setattr(
        runtime, "connection", SimpleNamespace(active_command_count=0)
    )
    monkeypatch.setattr(runtime, "_started", True)
    monkeypatch.setattr(
        "workgate.executor.overlay.detect_tailscale_overlay", lambda: None
    )
    session = SessionInventorySummary(
        session_id=SESSION, workdir=str(workspace)
    )
    monkeypatch.setattr(runtime.sessions, "inventory", lambda: (session,))
    observed = []

    async def authoritative_inventory(session_ids: frozenset[str]):
        observed.append(session_ids)
        return (
            ShellInventorySummary(shell_id="shell-1", session_id=SESSION),
        ), ()

    monkeypatch.setattr(
        runtime.shell, "reconnect_inventory", authoritative_inventory
    )
    with pytest.raises(RuntimeError, match="persistent shells or jobs"):
        await runtime.require_source_cutover_idle()
    assert observed == [frozenset({SESSION})]


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_durable_lost_job_full_cutover_inventory_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    """Real private jobs.json -> Hello projection -> native cutover guard."""
    from workgate.executor.jobs.shell import shell_job_inventory_snapshot

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=workspace, state_dir=tmp_path / "state")
        )
    )
    state_store = runtime.services.state_store
    jobs_path = state_store.layout.jobs_store_path
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    state_store.write_json(
        jobs_path,
        {
            "version": 2,
            "jobs": [
                {
                    "kind": "shell",
                    "session_id": SESSION,
                    "job_id": "job-historical",
                    "status": "lost",
                    "shell_id": "shell-gone",
                    "completed_at": 100.0,
                    "shell_absence_confirmed": confirmed,
                }
            ],
        },
    )
    session = SessionInventorySummary(
        session_id=SESSION, workdir=str(workspace)
    )
    monkeypatch.setattr(runtime.sessions, "inventory", lambda: (session,))
    monkeypatch.setattr(
        runtime, "connection", SimpleNamespace(active_command_count=0)
    )
    monkeypatch.setattr(runtime, "_started", True)
    monkeypatch.setattr(
        "workgate.executor.overlay.detect_tailscale_overlay", lambda: None
    )

    async def inventory(session_ids: frozenset[str]):
        jobs, _private_shells = shell_job_inventory_snapshot(session_ids)
        return (), jobs

    async def absent(*_args):
        return set()

    monkeypatch.setattr(runtime.shell, "reconnect_inventory", inventory)
    monkeypatch.setattr(
        "workgate.executor.shell.authoritative_persistent_shell_ids_execute",
        absent,
    )
    hello = await runtime._build_reconnect_hello()
    assert len(hello.jobs) == 1
    assert hello.jobs[0].status == "lost"
    assert hello.jobs[0]._shell_absence_confirmed is confirmed
    assert "shell_absence_confirmed" not in hello.model_dump_json()

    if confirmed:
        await runtime.require_source_cutover_idle()
    else:
        with pytest.raises(SourceCutoverDeferred, match="jobs present"):
            await runtime.require_source_cutover_idle()
