import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.helpers import python_shell_command
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.jobs import ExecutorJobService, runner_bootstrap
from workgate.executor.jobs import lifecycle as job_lifecycle
from workgate.executor.jobs import runner as job_runner
from workgate.executor.jobs import shell as job_shell
from workgate.executor.tool_session.store import get_tool_session_store
from workgate.jobs import managed as job_managed
from workgate.jobs import persistence as job_persistence
from workgate.jobs import recovery as job_recovery
from workgate.jobs import state as job_state
from workgate.jobs import status as job_status
from workgate.protocol.ids import new_session_id
from workgate.schemas.result_models.jobs import (
    JobInfo,
    JobListOutput,
    JobRetryOutput,
    JobStartOutput,
    JobStopOutput,
    JobTailOutput,
)
from workgate.schemas.result_models.shell import (
    KillPersistentShellOutput,
    ReadPersistentShellOutput,
    StartPersistentShellOutput,
)

pytestmark = pytest.mark.usefixtures("managed_jobs_runtime_owner")


def _load_store_untyped() -> dict[str, Any]:
    """Expose mutable durable rows only to tests that intentionally corrupt state."""
    return cast(dict[str, Any], job_persistence.load_store())


def _attempt_paths_untyped(job_id: str, attempt: int) -> dict[str, Path]:
    """Expose attempt paths as a homogeneous mapping for artifact tests."""
    return cast(dict[str, Path], job_persistence.attempt_paths(job_id, attempt))


def test_job_operation_cleanup_uses_one_authoritative_state_set():
    row: dict[str, object] = {}
    operation_id = job_state.begin_job_operation(row, "test")
    assert job_state.job_operation_is_active(row, "test")

    job_state.discard_job_operation(operation_id)
    assert not job_state.job_operation_is_active(row, "test")


def _create_session(workdir: str = ".") -> str:
    store = get_tool_session_store()
    return store.create_session(
        session_id=str(new_session_id()), workdir=workdir
    ).session_id


_TEST_JOB_DEPS = SimpleNamespace(get_store=get_tool_session_store)


def _executor_job_dependencies():
    return resolve_executor_config(get_settings()), _TEST_JOB_DEPS.get_store()


def _test_refresh_job_status(
    job: dict[str, Any],
    active_shells: set[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        job_lifecycle._refresh_job_status(
            job,
            active_shells,
            now,
            managed_job_has_local_task=job_managed._managed_job_has_local_task,
            managed_job_liveness=job_managed._managed_job_liveness,
            read_status=job_lifecycle._read_status,
            read_status_path=job_lifecycle._read_status_path,
        ),
    )


def _has_shell_job(session_id: str) -> bool:
    with job_shell._store_transaction() as store:
        rows = store.get("jobs", [])
        return any(
            isinstance(row, dict)
            and str(row.get("session_id") or "") == session_id
            and str(row.get("kind") or "shell") != "managed"
            for row in rows
        )


async def _test_job_start_execute(
    session_id: str,
    command: str,
    cwd: str = ".",
    name: str | None = None,
) -> JobStartOutput:
    async with job_shell.session_lifecycle_lock(session_id):
        return await job_shell.start_shell_job_unlocked(
            *_executor_job_dependencies(),
            session_id,
            command,
            cwd=cwd,
            name=name,
        )


async def _test_job_list_execute(
    session_id: str, include_finished: bool = True
) -> JobListOutput:
    managed = await job_managed.managed_job_list_execute(
        session_id, include_finished
    )
    shell = JobListOutput(jobs=[], counts={})
    if _has_shell_job(session_id):
        async with job_shell.session_lifecycle_lock(session_id):
            shell = await job_shell.list_shell_jobs_unlocked(
                *_executor_job_dependencies(),
                session_id,
                include_finished=include_finished,
            )
    rows = [*shell.jobs, *managed.jobs]
    rows.sort(key=lambda row: row.created_at, reverse=True)
    counts = dict(shell.counts)
    for status, count in managed.counts.items():
        counts[status] = counts.get(status, 0) + count
    return JobListOutput(jobs=rows, counts=counts)


async def _test_job_tail_execute(
    session_id: str, job_id: str, lines: int = 200
) -> JobTailOutput:
    if job_id in job_managed.managed_job_id_set(session_id, [job_id]):
        return await job_managed.managed_job_tail_execute(
            session_id, job_id, lines
        )
    async with job_shell.session_lifecycle_lock(session_id):
        return await job_shell.tail_shell_job_unlocked(
            *_executor_job_dependencies(), session_id, job_id, lines
        )


async def _test_job_stop_execute(session_id: str, job_id: str) -> JobStopOutput:
    async with job_shell.session_lifecycle_lock(session_id):
        if job_id in job_managed.managed_job_id_set(session_id, [job_id]):
            return await job_managed.stop_managed_job_without_session_admission(
                session_id, job_id
            )
        return await job_shell.stop_shell_job_unlocked(
            *_executor_job_dependencies(), session_id, job_id
        )


async def _test_job_retry_execute(
    session_id: str, job_id: str
) -> JobRetryOutput:
    async with job_shell.session_lifecycle_lock(session_id):
        if job_id in job_managed.managed_job_id_set(session_id, [job_id]):
            return (
                await job_managed.retry_managed_job_without_session_admission(
                    session_id, job_id
                )
            )
        return await job_shell.retry_shell_job_unlocked(
            *_executor_job_dependencies(), session_id, job_id
        )


def test_runner_command_quotes_powershell_arguments():
    command = job_lifecycle._runner_command(
        [
            r"C:\Program Files\Python\python.exe",
            "-m",
            "workgate.main",
            "--status-file",
            r"C:\state dir\job's-status.json",
        ],
        "powershell.exe",
    )

    assert command == (
        "& 'C:\\Program Files\\Python\\python.exe' '-m' "
        "'workgate.main' '--status-file' "
        "'C:\\state dir\\job''s-status.json'"
    )


def test_lifecycle_helpers_cover_platform_and_bounded_log_paths(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(job_lifecycle.sys, "frozen", True, raising=False)
    paths: job_state.JobAttemptPaths = {
        "command": tmp_path / "command.txt",
        "log": tmp_path / "job.log",
        "status": tmp_path / "status.json",
    }
    argv = job_lifecycle._runner_argv(
        resolve_executor_config(get_settings()), paths, tmp_path
    )
    assert argv[:2] == [job_lifecycle.sys.executable, "job-runner"]
    monkeypatch.setattr(job_lifecycle.sys, "frozen", False, raising=False)
    argv = job_lifecycle._runner_argv(
        resolve_executor_config(get_settings()), paths, tmp_path
    )
    assert argv[0] == job_lifecycle.sys.executable
    assert Path(argv[1]).name == "runner_bootstrap.py"
    assert Path(argv[1]).is_absolute()
    assert argv[2] == "--command-file"
    assert job_lifecycle._runner_command(
        ["echo", "hello world"], "cmd.exe"
    ) == ('echo "hello world"')

    paths["log"].write_bytes(b"abcdef\n")
    assert (
        job_status._read_log_tail(str(paths["log"]), 1, max_bytes=4) == "def\n"
    )


def test_runner_bootstrap_anchors_runtime_and_delegates(monkeypatch):
    delegated: list[bool] = []
    isolated_path = ["sentinel"]
    monkeypatch.setattr(runner_bootstrap.sys, "path", isolated_path)
    monkeypatch.setattr(job_runner, "main", lambda: delegated.append(True))

    runner_bootstrap.main()

    assert runner_bootstrap.sys.path[0] == str(
        Path(runner_bootstrap.__file__).resolve().parents[2]
    )
    assert delegated == [True]


def test_shell_backend_helper_contracts(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"exit_code": 0, "completed_at": 3.0}), encoding="utf-8"
    )

    assert job_shell._managed_job_has_local_task({}) is False
    assert job_shell._managed_job_liveness({}) == "dead"
    assert job_shell._read_status_path(status_path) == {
        "exit_code": 0,
        "completed_at": 3.0,
    }
    with pytest.raises(RuntimeError, match="managed job passed"):
        job_shell._refresh_job_status(
            {"kind": "managed", "status": "running"}, set()
        )


def _job_info(job_id: str = "job_1", session_id: str = "ABC12345") -> JobInfo:
    return JobInfo(
        job_id=job_id,
        name=job_id,
        status="running",
        command="echo ok",
        cwd=".",
        session_id=session_id,
        created_at=1.0,
        updated_at=1.0,
        last_started_at=1.0,
        attempts=1,
    )


@pytest.mark.asyncio
async def test_executor_job_execute_dispatches_companion_actions(monkeypatch):
    calls: list[tuple[Any, ...]] = []
    job = _job_info()
    service = ExecutorJobService(
        resolve_executor_config(get_settings()), get_tool_session_store()
    )

    async def fake_list(session_id: str, include_finished: bool = True):
        calls.append(("list", session_id, include_finished))
        return JobListOutput(jobs=[job], counts={"running": 1})

    async def fake_tail(session_id: str, job_id: str, lines: int = 200):
        calls.append(("poll", session_id, job_id, lines))
        return JobTailOutput(job=job, output=f"{job_id}:{lines}")

    async def fake_stop(session_id: str, job_id: str):
        calls.append(("cancel", session_id, job_id))
        return JobStopOutput(job=job, killed=True, stderr="")

    async def fake_retry(session_id: str, job_id: str):
        calls.append(("retry", session_id, job_id))
        data = job.model_dump()
        data.update(job_id=job_id, attempts=2)
        return JobRetryOutput.model_validate(data)

    monkeypatch.setattr(service, "list", fake_list)
    monkeypatch.setattr(service, "tail", fake_tail)
    monkeypatch.setattr(service, "stop", fake_stop)
    monkeypatch.setattr(service, "retry", fake_retry)

    listed = await service.execute(
        {"session_id": "ABC12345", "include_finished": False}
    )
    assert listed.operation == "list"
    assert listed.jobs == [job]
    assert listed.counts == {"running": 1}
    assert calls == [("list", "ABC12345", False)]

    calls.clear()
    polled = await service.execute(
        {"session_id": "ABC12345", "poll": ["job_1", "job_2"], "lines": 5}
    )
    assert [entry.output for entry in polled.outputs] == ["job_1:5", "job_2:5"]
    assert calls == [
        ("poll", "ABC12345", "job_1", 5),
        ("poll", "ABC12345", "job_2", 5),
    ]

    calls.clear()
    cancelled = await service.execute(
        {"session_id": "ABC12345", "cancel": ["job_1"]}
    )
    assert cancelled.cancelled[0].killed is True
    assert calls == [("cancel", "ABC12345", "job_1")]

    calls.clear()
    retried = await service.execute(
        {"session_id": "ABC12345", "retry": ["job_1"]}
    )
    assert retried.retried[0].attempts == 2
    assert calls == [("retry", "ABC12345", "job_1")]


@pytest.mark.asyncio
async def test_executor_job_execute_rejects_combined_actions():
    service = ExecutorJobService(
        resolve_executor_config(get_settings()), get_tool_session_store()
    )
    with pytest.raises(ValueError, match="list_jobs cannot be combined"):
        await service.execute(
            {"session_id": "ABC12345", "list_jobs": True, "poll": ["job_1"]}
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        await service.execute(
            {
                "session_id": "ABC12345",
                "poll": ["job_1"],
                "cancel": ["job_2"],
            }
        )


@pytest.mark.asyncio
async def test_tracked_job_lifecycle_with_backing_shells(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    session_id = _create_session()

    active_sessions: set[str] = set()
    session_counter = 0

    async def fake_start_shell(
        _config,
        _store,
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == session_id
        nonlocal session_counter
        session_counter += 1
        shell_id = f"shell_{session_counter}"
        active_sessions.add(shell_id)
        return StartPersistentShellOutput.model_validate(
            {
                "shell_id": shell_id,
                "name": name,
                "cwd": cwd,
                "command": command,
                "backend": "fake",
            }
        )

    async def fake_active_shell_ids(*_args):
        return set(active_sessions)

    async def fake_read_shell(_config, shell_id: str, lines: int):
        return ReadPersistentShellOutput(
            shell_id=shell_id, output=f"tail {shell_id}\n", lines=lines
        )

    async def fake_kill_shell(_config, _store, shell_id: str):
        active_sessions.discard(shell_id)
        return KillPersistentShellOutput(
            shell_id=shell_id, killed=True, stderr=""
        )

    monkeypatch.setattr(
        job_shell, "start_persistent_shell_execute", fake_start_shell
    )
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        fake_active_shell_ids,
    )
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        fake_active_shell_ids,
    )
    monkeypatch.setattr(
        job_shell, "read_persistent_shell_output_execute", fake_read_shell
    )
    monkeypatch.setattr(
        job_shell, "kill_persistent_shell_execute", fake_kill_shell
    )

    started = await _test_job_start_execute(
        session_id, "python -m http.server", ".", "serve"
    )

    assert started.name == "serve"
    assert started.status == "running"
    assert started.session_id == session_id
    assert started.cwd == str(tmp_path)
    assert started.attempts == 1
    assert started.kind == "shell"

    listed = await _test_job_list_execute(session_id)
    assert listed.counts == {"running": 1}
    assert listed.jobs[0].job_id == started.job_id
    assert listed.jobs[0].session_id == session_id

    tailed = await _test_job_tail_execute(session_id, started.job_id, lines=5)
    assert tailed.output == "tail shell_1\n"
    assert tailed.job.status == "running"
    assert tailed.job.session_id == session_id

    stopped = await _test_job_stop_execute(session_id, started.job_id)
    assert stopped.killed is True
    assert stopped.job.status == "stopped"

    running_only = await _test_job_list_execute(
        session_id, include_finished=False
    )
    assert running_only.jobs == []
    assert running_only.counts == {"stopped": 1}

    retried = await _test_job_retry_execute(session_id, started.job_id)
    assert retried.status == "running"
    assert retried.attempts == 2
    assert retried.session_id == session_id


@pytest.mark.asyncio
async def test_tracked_jobs_are_isolated_by_agent_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    first_session = _create_session()
    second_session = _create_session()

    async def fake_start_shell(
        _config,
        _store,
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == first_session
        return StartPersistentShellOutput.model_validate(
            {"shell_id": f"shell-{name}", "name": name, "cwd": cwd}
        )

    async def fake_list_shells():
        return {"shell-first-job"}

    monkeypatch.setattr(
        job_shell, "start_persistent_shell_execute", fake_start_shell
    )

    started = await _test_job_start_execute(
        first_session, "sleep 60", ".", "first-job"
    )

    first_list = await _test_job_list_execute(first_session)
    second_list = await _test_job_list_execute(second_session)

    assert [job.job_id for job in first_list.jobs] == [started.job_id]
    assert second_list.jobs == []
    assert second_list.counts == {}

    with pytest.raises(KeyError, match="job not found in session"):
        await _test_job_tail_execute(second_session, started.job_id)
    with pytest.raises(KeyError, match="job not found in session"):
        await _test_job_stop_execute(second_session, started.job_id)
    with pytest.raises(KeyError, match="job not found in session"):
        await _test_job_retry_execute(second_session, started.job_id)


@pytest.mark.asyncio
async def test_tracked_job_is_lost_when_shell_disappears_without_status(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    session_id = _create_session()

    async def fake_start_shell(
        _config,
        _store,
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == session_id
        return StartPersistentShellOutput(
            shell_id="missing-shell", name=name, cwd=cwd, command=command
        )

    async def no_active_shell_ids(*_args):
        return set()

    monkeypatch.setattr(
        job_shell, "start_persistent_shell_execute", fake_start_shell
    )
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        no_active_shell_ids,
    )
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        no_active_shell_ids,
    )

    started = await _test_job_start_execute(session_id, "echo done", ".", None)
    listed = await _test_job_list_execute(session_id)

    assert listed.jobs[0].job_id == started.job_id
    assert listed.jobs[0].status == "lost"
    assert listed.jobs[0].session_id == session_id
    assert listed.counts == {"lost": 1}


def _configure_job_state(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    for name, value in overrides.items():
        monkeypatch.setenv(f"WORKGATE_{name.upper()}", str(value))
    clear_settings_cache()
    get_tool_session_store().clear()


def test_job_runner_persists_bounded_output_and_terminal_status(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    paths = _attempt_paths_untyped("job_runner", 1)
    command = (
        "echo prefix-" + "x" * 100 + "-suffix"
        if os.name == "nt"
        else python_shell_command("print('prefix-' + 'x' * 100 + '-suffix')")
    )
    paths["command"].write_text(command, encoding="utf-8")
    args = SimpleNamespace(
        command_file=str(paths["command"]),
        log_file=str(paths["log"]),
        status_file=str(paths["status"]),
        cwd=str(tmp_path),
        shell=os.environ.get("COMSPEC", "cmd.exe")
        if os.name == "nt"
        else "/bin/bash",
        max_log_bytes=32,
    )

    with pytest.raises(SystemExit) as exit_info:
        job_runner.run_job_runner_from_args(args)

    status = json.loads(paths["status"].read_text(encoding="utf-8"))
    output = paths["log"].read_text(encoding="utf-8")
    assert exit_info.value.code == 0
    assert status["exit_code"] == 0
    assert status["error"] is None
    assert status["log_truncated"] is True
    assert status["output_bytes"] > len(output.encode())
    assert output.rstrip().endswith("-suffix")
    assert len(output.encode()) <= 32


def test_job_runner_records_nonzero_exit(tmp_path, monkeypatch):
    _configure_job_state(tmp_path, monkeypatch)
    paths = _attempt_paths_untyped("job_failed", 1)
    command = (
        "echo failed-output & exit /b 7"
        if os.name == "nt"
        else python_shell_command(
            "import sys; print('failed-output'); sys.exit(7)"
        )
    )
    paths["command"].write_text(command, encoding="utf-8")
    args = SimpleNamespace(
        command_file=str(paths["command"]),
        log_file=str(paths["log"]),
        status_file=str(paths["status"]),
        cwd=str(tmp_path),
        shell=os.environ.get("COMSPEC", "cmd.exe")
        if os.name == "nt"
        else "/bin/bash",
        max_log_bytes=1024,
    )

    with pytest.raises(SystemExit) as exit_info:
        job_runner.run_job_runner_from_args(args)

    status = json.loads(paths["status"].read_text(encoding="utf-8"))
    output_lines = paths["log"].read_text(encoding="utf-8").splitlines()
    assert exit_info.value.code == 7
    assert status["exit_code"] == 7
    assert status["error"] is None
    assert [line.rstrip() for line in output_lines] == ["failed-output"]


def test_job_runner_shell_args_cover_windows_shell_families():
    assert job_runner._runner_shell_args("pwsh", "Write-Output ok") == [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Write-Output ok",
    ]
    assert job_runner._runner_shell_args("cmd.exe", "echo ok") == [
        "cmd.exe",
        "/D",
        "/S",
        "/C",
        "echo ok",
    ]


def test_job_runner_records_startup_error_before_process_spawn(tmp_path):
    status_file = tmp_path / "status.json"
    args = SimpleNamespace(
        command_file=str(tmp_path / "missing-command"),
        log_file=str(tmp_path / "runner.log"),
        status_file=str(status_file),
        cwd=str(tmp_path),
        shell="/bin/bash",
        max_log_bytes=1024,
    )

    with pytest.raises(SystemExit) as exit_info:
        job_runner.run_job_runner_from_args(args)

    status = json.loads(status_file.read_text(encoding="utf-8"))
    assert exit_info.value.code == 1
    assert status["exit_code"] is None
    assert status["error"].startswith("FileNotFoundError:")


@pytest.mark.asyncio
async def test_terminal_job_output_remains_available_after_shell_exit(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()
    paths = _attempt_paths_untyped("job_done", 1)
    paths["log"].write_text("durable output\n", encoding="utf-8")
    paths["status"].write_text(
        json.dumps(
            {
                "exit_code": 0,
                "completed_at": 5.0,
                "error": None,
                "log_truncated": False,
                "output_bytes": 15,
            }
        ),
        encoding="utf-8",
    )
    job_persistence.save_store(
        {
            "version": job_persistence.JOB_STORE_VERSION,
            "jobs": [
                {
                    "job_id": "job_done",
                    "name": "done",
                    "status": "running",
                    "command": "echo durable output",
                    "cwd": str(tmp_path),
                    "session_id": session_id,
                    "shell_id": "gone-shell",
                    "log_path": str(paths["log"]),
                    "status_path": str(paths["status"]),
                    "created_at": 1.0,
                    "updated_at": 2.0,
                    "last_started_at": 2.0,
                    "attempts": 1,
                }
            ],
        }
    )

    async def no_shells(*_args):
        return set()

    result = await _test_job_tail_execute(session_id, "job_done", lines=20)

    assert result.job.status == "succeeded"
    assert result.job.exit_code == 0
    assert result.job.completed_at == 5.0
    assert result.output == "durable output\n"
    assert result.message == "job completed with exit code 0"


def test_job_store_recovers_from_backup_and_migrates_v1(tmp_path, monkeypatch):
    _configure_job_state(tmp_path, monkeypatch)
    job_persistence.job_store_path().parent.mkdir(parents=True, exist_ok=True)
    job_persistence.job_store_path().write_text("{broken", encoding="utf-8")
    job_persistence.job_store_backup_path().write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "job_id": "legacy",
                        "status": "stopped",
                        "session_id": "ABC12345",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = _load_store_untyped()

    assert store["version"] == job_persistence.JOB_STORE_VERSION
    assert store["jobs"][0]["job_id"] == "legacy"


def test_job_store_refuses_to_reset_when_main_and_backup_are_corrupt(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    job_persistence.job_store_path().parent.mkdir(parents=True, exist_ok=True)
    job_persistence.job_store_path().write_text("{broken", encoding="utf-8")
    job_persistence.job_store_backup_path().write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to reset"):
        _load_store_untyped()


def test_job_store_retention_keeps_active_and_newest_finished(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch, max_jobs=2)
    old_paths = _attempt_paths_untyped("old", 1)
    new_paths = _attempt_paths_untyped("new", 1)
    for path in [*old_paths.values(), *new_paths.values()]:
        path.write_text("artifact", encoding="utf-8")
    store = {
        "version": job_persistence.JOB_STORE_VERSION,
        "jobs": [
            {
                "job_id": "active",
                "status": "running",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
            {
                "job_id": "old",
                "status": "failed",
                "created_at": 2.0,
                "updated_at": 2.0,
            },
            {
                "job_id": "new",
                "status": "succeeded",
                "created_at": 3.0,
                "updated_at": 3.0,
            },
        ],
    }

    job_persistence.save_store(store)
    persisted = _load_store_untyped()

    assert {row["job_id"] for row in persisted["jobs"]} == {"active", "new"}
    assert not any(path.exists() for path in old_paths.values())
    assert all(path.exists() for path in new_paths.values())


def test_job_store_retention_keeps_unconfirmed_lost_shell(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch, max_jobs=1)
    store = {
        "version": job_persistence.JOB_STORE_VERSION,
        "jobs": [
            {
                "job_id": "lost-live",
                "kind": "shell",
                "status": "lost",
                "shell_id": "shell-unknown",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
            {
                "job_id": "finished",
                "kind": "shell",
                "status": "succeeded",
                "shell_absence_confirmed": True,
                "created_at": 2.0,
                "updated_at": 2.0,
            },
        ],
    }

    job_persistence.save_store(store)

    assert [row["job_id"] for row in _load_store_untyped()["jobs"]] == [
        "lost-live"
    ]


@pytest.mark.asyncio
async def test_job_start_failure_is_persisted_as_failed(tmp_path, monkeypatch):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()

    async def fail_start(
        _config,
        _store,
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == session_id
        raise RuntimeError("tmux unavailable")

    monkeypatch.setattr(job_shell, "start_persistent_shell_execute", fail_start)

    with pytest.raises(RuntimeError, match="tmux unavailable"):
        await _test_job_start_execute(session_id, "echo hello")

    rows = _load_store_untyped()["jobs"]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["completed_at"] is not None
    assert rows[0]["error"] == "start failed: RuntimeError: tmux unavailable"


@pytest.mark.asyncio
async def test_interrupted_start_recovers_active_shell(tmp_path, monkeypatch):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()
    job_persistence.save_store(
        {
            "version": job_persistence.JOB_STORE_VERSION,
            "jobs": [
                {
                    "job_id": "recover",
                    "name": "recover",
                    "status": "starting",
                    "command": "sleep 1",
                    "cwd": str(tmp_path),
                    "session_id": session_id,
                    "shell_id": "recover-shell",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                    "last_started_at": None,
                    "attempts": 1,
                    "operation_id": "stale-start",
                    "operation_kind": "start",
                }
            ],
        }
    )

    async def active_shell_ids(*_args):
        return {"recover-shell"}

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        active_shell_ids,
    )

    result = await _test_job_list_execute(session_id)

    assert result.jobs[0].status == "running"
    assert result.jobs[0].last_started_at is not None
    assert "recovered job start" in (result.jobs[0].error or "")


@pytest.mark.asyncio
async def test_job_retry_failure_is_persisted_and_clears_pending_state(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()
    job_persistence.save_store(
        {
            "version": job_persistence.JOB_STORE_VERSION,
            "jobs": [
                {
                    "job_id": "retry-failure",
                    "name": "retry-failure",
                    "status": "failed",
                    "command": "echo retry",
                    "cwd": str(tmp_path),
                    "session_id": session_id,
                    "created_at": 1.0,
                    "updated_at": 2.0,
                    "last_started_at": 1.0,
                    "completed_at": 2.0,
                    "attempts": 1,
                }
            ],
        }
    )

    async def no_shells(*_args):
        return set()

    async def fail_start(
        _config,
        _store,
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == session_id
        raise RuntimeError("retry shell unavailable")

    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", no_shells
    )
    monkeypatch.setattr(job_shell, "start_persistent_shell_execute", fail_start)

    with pytest.raises(RuntimeError, match="retry shell unavailable"):
        await _test_job_retry_execute(session_id, "retry-failure")

    job = _load_store_untyped()["jobs"][0]
    assert job["status"] == "failed"
    assert job["attempts"] == 2
    assert job["completed_at"] is not None
    assert job["error"] == (
        "retry failed: RuntimeError: retry shell unavailable"
    )
    assert not any(key.startswith("pending_") for key in job)
    assert "operation_id" not in job


@pytest.mark.asyncio
async def test_interrupted_retry_adopts_active_pending_attempt(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()
    paths = _attempt_paths_untyped("retry-recover", 2)
    job_persistence.save_store(
        {
            "version": job_persistence.JOB_STORE_VERSION,
            "jobs": [
                {
                    "job_id": "retry-recover",
                    "name": "retry-recover",
                    "status": "retrying",
                    "command": "sleep 1",
                    "cwd": str(tmp_path),
                    "session_id": session_id,
                    "shell_id": "old-shell",
                    "created_at": 1.0,
                    "updated_at": 2.0,
                    "last_started_at": 1.0,
                    "attempts": 1,
                    "pending_attempt": 2,
                    "pending_shell_id": "retry-shell",
                    "pending_command_path": str(paths["command"]),
                    "pending_log_path": str(paths["log"]),
                    "pending_status_path": str(paths["status"]),
                    "operation_id": "stale-retry",
                    "operation_kind": "retry",
                }
            ],
        }
    )

    async def active_shell_ids(*_args):
        return {"retry-shell"}

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        active_shell_ids,
    )

    result = await _test_job_list_execute(session_id)

    recovered = result.jobs[0]
    assert recovered.status == "running"
    assert recovered.attempts == 2
    assert recovered.last_started_at is not None
    assert "recovered retry" in (recovered.error or "")
    job = _load_store_untyped()["jobs"][0]
    assert job["shell_id"] == "retry-shell"
    assert job["log_path"] == str(paths["log"])
    assert not any(key.startswith("pending_") for key in job)
    assert "operation_id" not in job


def test_concurrent_job_store_transactions_do_not_lose_records(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch, max_jobs=100)

    def append(index: int) -> None:
        with job_recovery.store_transaction() as store:
            store["jobs"].append(
                {
                    "job_id": f"job-{index}",
                    "status": "succeeded",
                    "created_at": float(index),
                    "updated_at": float(index),
                }
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(32)))

    persisted = _load_store_untyped()["jobs"]
    assert {row["job_id"] for row in persisted} == {
        f"job-{index}" for index in range(32)
    }


@pytest.mark.asyncio
async def test_job_start_does_not_launch_shell_for_invalid_store(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()
    invalid = json.dumps({"version": 99, "jobs": []})
    job_persistence.job_store_path().parent.mkdir(parents=True, exist_ok=True)
    job_persistence.job_store_path().write_text(invalid, encoding="utf-8")
    job_persistence.job_store_backup_path().write_text(
        invalid, encoding="utf-8"
    )
    started = False

    async def fake_start(
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == session_id
        nonlocal started
        started = True
        return StartPersistentShellOutput(
            shell_id="must-not-start", name=name, cwd=cwd, command=command
        )

    monkeypatch.setattr(job_shell, "start_persistent_shell_execute", fake_start)

    with pytest.raises(RuntimeError, match="refusing to reset"):
        await _test_job_start_execute(session_id, "echo must-not-run")

    assert started is False
    runtime_dir = job_persistence.job_runtime_dir()
    assert not list(runtime_dir.iterdir())


@pytest.mark.asyncio
async def test_job_start_kills_shell_when_running_state_cannot_be_committed(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()
    killed: list[str] = []

    async def corrupt_after_launch(
        _config,
        _store,
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == session_id
        invalid = json.dumps({"version": 99, "jobs": []})
        job_persistence.job_store_path().write_text(invalid, encoding="utf-8")
        job_persistence.job_store_backup_path().write_text(
            invalid, encoding="utf-8"
        )
        return StartPersistentShellOutput(
            shell_id="launched-shell", name=name, cwd=cwd, command=command
        )

    async def fake_kill(_config, _store, shell_id: str):
        killed.append(shell_id)
        return KillPersistentShellOutput(
            shell_id=shell_id, killed=True, stderr=""
        )

    monkeypatch.setattr(
        job_shell, "start_persistent_shell_execute", corrupt_after_launch
    )
    monkeypatch.setattr(job_shell, "kill_persistent_shell_execute", fake_kill)

    with pytest.raises(RuntimeError, match="refusing to reset"):
        await _test_job_start_execute(session_id, "echo launched")

    assert killed == ["launched-shell"]
    assert not list(job_persistence.job_runtime_dir().iterdir())


@pytest.mark.asyncio
async def test_managed_job_tracks_progress_stop_retry_and_result(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()
    release = asyncio.Event()

    async def handler(context, payload):
        await context.log(f"started {payload['value']}")
        await context.update_progress(phase="waiting", value=payload["value"])
        await release.wait()
        await context.log("finished")
        return {"value": payload["value"]}

    job_managed.register_managed_job_handler("test-managed-lifecycle", handler)

    started = await job_managed.start_managed_job(
        session_id,
        "test-managed-lifecycle",
        {"value": 7},
        name="managed",
        command="managed test",
    )
    assert started.kind == "managed"
    assert started.status == "running"

    tailed = None
    for _ in range(100):
        await asyncio.sleep(0.01)
        tailed = await _test_job_tail_execute(session_id, started.job_id)
        if tailed.job.progress == {"phase": "waiting", "value": 7}:
            break
    assert tailed is not None
    assert "started 7" in tailed.output
    assert tailed.job.progress == {"phase": "waiting", "value": 7}

    stopped = await _test_job_stop_execute(session_id, started.job_id)
    assert stopped.killed is True
    assert stopped.job.status == "stopped"

    retried = await _test_job_retry_execute(session_id, started.job_id)
    assert retried.kind == "managed"
    assert retried.status == "running"
    assert retried.attempts == 2
    release.set()

    current = retried
    for _ in range(100):
        await asyncio.sleep(0.01)
        current = (await _test_job_list_execute(session_id)).jobs[0]
        if current.status == "succeeded":
            break
    assert current.status == "succeeded"
    assert current.result == {"value": 7}
    assert current.progress == {"phase": "waiting", "value": 7}


@pytest.mark.asyncio
async def test_managed_reference_stop_cancels_job_owned_by_source_session(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    source_session_id = _create_session()
    destination_session_id = "DEST0001"
    entered = asyncio.Event()

    async def handler(_context, _payload):
        entered.set()
        await asyncio.Event().wait()

    job_managed.register_managed_job_handler("test-managed-reference", handler)
    started = await job_managed.start_managed_job(
        source_session_id,
        "test-managed-reference",
        {"dst_session_id": destination_session_id},
    )
    await entered.wait()

    stopped = await job_managed.job_stop_managed_references_execute(
        destination_session_id,
        managed_kind="test-managed-reference",
        payload_key="dst_session_id",
    )

    assert stopped == [started.job_id]
    current = (await _test_job_list_execute(source_session_id)).jobs[0]
    assert current.status == "stopped"
    assert job_managed.managed_job_referenced_session_ids(
        source_session_id, started.job_id
    ) == tuple(sorted((source_session_id, destination_session_id)))


@pytest.mark.asyncio
async def test_managed_reference_stop_filters_unrelated_rows(monkeypatch):
    unrelated_shell = {
        "kind": "shell",
        "status": "running",
        "job_id": "unrelated-shell",
        "shell_id": "live-shell",
    }
    rows = [
        "not-a-row",
        unrelated_shell,
        {
            "kind": "managed",
            "managed_kind": "other",
            "status": "running",
        },
        {
            "kind": "managed",
            "managed_kind": "copy",
            "status": "stopped",
        },
        {
            "kind": "managed",
            "managed_kind": "copy",
            "status": "running",
            "managed_payload": None,
        },
        {
            "kind": "managed",
            "managed_kind": "copy",
            "status": "running",
            "managed_payload": {"dst_session_id": "OTHER"},
        },
        {
            "kind": "managed",
            "managed_kind": "copy",
            "status": "running",
            "managed_payload": {"dst_session_id": "DEST0001"},
            "session_id": "",
            "job_id": "missing-owner",
        },
        {
            "kind": "managed",
            "managed_kind": "copy",
            "status": "running",
            "managed_payload": {"dst_session_id": "DEST0001"},
            "session_id": "SOURCE01",
            "job_id": "job_target",
        },
    ]

    @contextmanager
    def fake_transaction():
        yield {"jobs": rows}

    async def fake_stop(session_id: str, job_id: str):
        assert (session_id, job_id) == ("SOURCE01", "job_target")
        return SimpleNamespace(
            killed=False, job=SimpleNamespace(status="succeeded")
        )

    refreshed: list[str] = []

    def fake_refresh(row, _active):
        refreshed.append(str(row.get("job_id") or ""))
        if row is unrelated_shell:
            row["status"] = "lost"
        return row

    monkeypatch.setattr(job_managed, "_store_transaction", fake_transaction)
    monkeypatch.setattr(job_managed, "_refresh_job_status", fake_refresh)
    monkeypatch.setattr(
        job_managed, "_stop_managed_job_without_session_admission", fake_stop
    )

    assert await job_managed.job_stop_managed_references_execute(
        "DEST0001", managed_kind="copy", payload_key="dst_session_id"
    ) == ["job_target"]
    assert unrelated_shell["status"] == "running"
    assert refreshed == ["missing-owner", "job_target"]


@pytest.mark.asyncio
async def test_reconcile_shell_jobs_marks_only_missing_shells_terminal(
    monkeypatch,
):
    stale = {
        "kind": "shell",
        "status": "running",
        "job_id": "stale-job",
        "shell_id": "missing-shell",
    }
    live = {
        "kind": "shell",
        "status": "running",
        "job_id": "live-job",
        "shell_id": "live-shell",
    }
    managed = {
        "kind": "managed",
        "status": "running",
        "job_id": "managed-job",
    }
    store = {"jobs": [stale, live, managed]}

    @contextmanager
    def fake_transaction():
        yield store

    async def fake_inventory(*_args):
        return {"live-shell"}

    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", fake_inventory
    )
    monkeypatch.setattr(
        job_shell, "_prune_store", lambda _store, **_kwargs: None
    )

    assert (
        await job_shell.reconcile_shell_jobs_execute(
            *_executor_job_dependencies()
        )
        is True
    )
    assert stale["status"] == "lost"
    assert live["status"] == "running"
    assert managed["status"] == "running"


@pytest.mark.asyncio
async def test_reconcile_shell_jobs_samples_inventory_under_owner_lock(
    monkeypatch,
):
    row = {
        "kind": "shell",
        "status": "starting",
        "job_id": "peer-start",
        "session_id": "SESSION1",
        "shell_id": "pending-shell",
    }
    store = {"jobs": [row]}
    held: list[str] = []
    active_locks: list[str] = []
    inventory_lock_states: list[tuple[str, ...]] = []

    @contextmanager
    def fake_transaction():
        yield store

    @asynccontextmanager
    async def fake_lifecycle_lock(session_id: str):
        held.append(session_id)
        active_locks.append(session_id)
        row["status"] = "running"
        try:
            yield
        finally:
            active_locks.remove(session_id)

    async def fake_inventory(*_args):
        inventory_lock_states.append(tuple(active_locks))
        return {"pending-shell"} if active_locks else set()

    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "session_lifecycle_lock", fake_lifecycle_lock
    )
    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", fake_inventory
    )
    monkeypatch.setattr(
        job_shell, "_prune_store", lambda _store, **_kwargs: None
    )

    assert (
        await job_shell.reconcile_shell_jobs_execute(
            *_executor_job_dependencies()
        )
        is True
    )
    assert held == ["SESSION1"]
    assert inventory_lock_states == [("SESSION1",), ()]
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_reconcile_shell_jobs_is_conservative_without_inventory(
    monkeypatch,
):
    row = {
        "kind": "shell",
        "status": "running",
        "job_id": "unknown-job",
        "shell_id": "unknown-shell",
    }
    store = {"jobs": [row]}

    @contextmanager
    def fake_transaction():
        yield store

    async def uncertain_inventory(*_args):
        return None

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "_prune_store", lambda _store, **_kwargs: None
    )

    assert (
        await job_shell.reconcile_shell_jobs_execute(
            *_executor_job_dependencies()
        )
        is False
    )
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_reconcile_shell_jobs_applies_durable_completion_without_inventory(
    monkeypatch,
):
    row = {
        "kind": "shell",
        "status": "running",
        "job_id": "completed-job",
        "shell_id": "unknown-shell",
    }
    store = {"jobs": [row]}

    @contextmanager
    def fake_transaction():
        yield store

    async def uncertain_inventory(*_args):
        return None

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "_prune_store", lambda _store, **_kwargs: None
    )
    monkeypatch.setattr(
        job_shell,
        "_read_status",
        lambda _job: {
            "exit_code": 0,
            "completed_at": 5.0,
            "error": None,
            "log_truncated": False,
            "output_bytes": 0,
        },
    )

    assert (
        await job_shell.reconcile_shell_jobs_execute(
            *_executor_job_dependencies()
        )
        is False
    )
    assert row["status"] == "succeeded"
    assert row["exit_code"] == 0
    assert row["completed_at"] == 5.0


@pytest.mark.asyncio
async def test_stop_shell_job_keeps_lost_job_when_inventory_is_uncertain(
    monkeypatch,
):
    row = {
        "kind": "shell",
        "status": "lost",
        "job_id": "lost-job",
        "session_id": "SESSION1",
        "shell_id": "lost-shell",
    }
    store = {"jobs": [row]}

    @contextmanager
    def fake_transaction():
        yield store

    async def uncertain_inventory(*_args):
        return None

    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )
    monkeypatch.setattr(
        job_shell,
        "kill_persistent_shell_execute",
        lambda *_args, **_kwargs: pytest.fail(
            "uncertain lost jobs must not be killed"
        ),
    )

    result = await job_shell.stop_shell_job_unlocked(
        *_executor_job_dependencies(), "SESSION1", "lost-job"
    )

    assert result.killed is False
    assert result.job.status == "lost"


@pytest.mark.asyncio
async def test_stop_shell_job_returns_terminal_job_without_killing(
    monkeypatch,
):
    row = {
        "kind": "shell",
        "status": "failed",
        "job_id": "failed-job",
        "session_id": "SESSION1",
        "shell_id": "failed-shell",
    }
    store = {"jobs": [row]}

    @contextmanager
    def fake_transaction():
        yield store

    async def empty_inventory(*_args):
        return set()

    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        empty_inventory,
    )
    monkeypatch.setattr(
        job_shell,
        "kill_persistent_shell_execute",
        lambda *_args, **_kwargs: pytest.fail(
            "terminal jobs must not be killed"
        ),
    )

    result = await job_shell.stop_shell_job_unlocked(
        *_executor_job_dependencies(), "SESSION1", "failed-job"
    )

    assert result.killed is False
    assert result.job.status == "failed"


@pytest.mark.asyncio
async def test_stop_shell_job_rejects_managed_row(monkeypatch):
    store = {
        "jobs": [
            {
                "kind": "managed",
                "status": "running",
                "job_id": "managed-job",
                "session_id": "SESSION1",
            }
        ]
    }

    @contextmanager
    def fake_transaction():
        yield store

    async def empty_inventory(*_args):
        return set()

    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        empty_inventory,
    )

    with pytest.raises(RuntimeError, match="not shell-backed"):
        await job_shell.stop_shell_job_unlocked(
            *_executor_job_dependencies(), "SESSION1", "managed-job"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "lost"])
async def test_managed_reference_stop_rejects_unconfirmed_result(
    status, monkeypatch
):
    row = {
        "kind": "managed",
        "managed_kind": "copy",
        "status": "running",
        "managed_payload": {"dst_session_id": "DEST0001"},
        "session_id": "SOURCE01",
        "job_id": "job_target",
    }

    @contextmanager
    def fake_transaction():
        yield {"jobs": [row]}

    async def fake_stop(_session_id: str, _job_id: str):
        return SimpleNamespace(killed=False, job=SimpleNamespace(status=status))

    monkeypatch.setattr(job_managed, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_managed, "_refresh_job_status", lambda value, _active: value
    )
    monkeypatch.setattr(
        job_managed, "_stop_managed_job_without_session_admission", fake_stop
    )

    with pytest.raises(RuntimeError, match=f"status='{status}'"):
        await job_managed.job_stop_managed_references_execute(
            "DEST0001", managed_kind="copy", payload_key="dst_session_id"
        )


@pytest.mark.asyncio
async def test_job_list_samples_inventory_under_owner_lock_only(
    monkeypatch,
):
    requested = {
        "job_id": "requested-job",
        "kind": "shell",
        "name": "requested-job",
        "status": "starting",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION1",
        "shell_id": "requested-shell",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    unrelated = {
        "job_id": "unrelated-job",
        "kind": "shell",
        "name": "unrelated-job",
        "status": "starting",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION2",
        "shell_id": "unrelated-shell",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    store = {"jobs": [requested, unrelated]}
    active_locks: list[str] = []
    inventory_lock_states: list[tuple[str, ...]] = []

    @contextmanager
    def fake_transaction():
        yield store

    @asynccontextmanager
    async def fake_lifecycle_lock(session_id: str):
        active_locks.append(session_id)
        requested["status"] = "running"
        try:
            yield
        finally:
            active_locks.remove(session_id)

    async def fake_inventory(*_args):
        inventory_lock_states.append(tuple(active_locks))
        return {"requested-shell"} if active_locks else set()

    monkeypatch.setattr(
        _TEST_JOB_DEPS,
        "get_store",
        lambda: SimpleNamespace(touch_session=lambda _session_id: None),
    )
    monkeypatch.setattr(
        job_shell, "session_lifecycle_lock", fake_lifecycle_lock
    )
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "_prune_store", lambda _store, **_kwargs: None
    )
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        fake_inventory,
    )

    result = await _test_job_list_execute("SESSION1")

    assert inventory_lock_states == [("SESSION1",)]
    assert [job.job_id for job in result.jobs] == ["requested-job"]
    assert requested["status"] == "running"
    assert unrelated["status"] == "starting"


@pytest.mark.asyncio
async def test_job_list_preserves_running_state_when_inventory_is_uncertain(
    monkeypatch,
):
    row = {
        "job_id": "job_one",
        "kind": "shell",
        "name": "job_one",
        "status": "running",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION1",
        "shell_id": "shell_one",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    store = {"jobs": [row]}

    @contextmanager
    def fake_transaction():
        yield store

    async def uncertain_inventory(*_args):
        return None

    monkeypatch.setattr(
        _TEST_JOB_DEPS,
        "get_store",
        lambda: SimpleNamespace(touch_session=lambda _session_id: None),
    )
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "_prune_store", lambda _store, **_kwargs: None
    )
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )

    result = await _test_job_list_execute("SESSION1")

    assert result.jobs[0].status == "running"
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_job_list_applies_durable_completion_when_inventory_is_uncertain(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session(str(tmp_path))
    paths = _attempt_paths_untyped("job_durable", 1)
    paths["status"].write_text(
        json.dumps(
            {
                "exit_code": 0,
                "completed_at": 5.0,
                "error": None,
                "log_truncated": False,
                "output_bytes": 0,
            }
        ),
        encoding="utf-8",
    )
    job_persistence.save_store(
        {
            "version": job_persistence.JOB_STORE_VERSION,
            "jobs": [
                {
                    "job_id": "job_durable",
                    "kind": "shell",
                    "name": "durable",
                    "status": "running",
                    "command": "echo done",
                    "cwd": str(tmp_path),
                    "session_id": session_id,
                    "shell_id": "shell_durable",
                    "status_path": str(paths["status"]),
                    "created_at": 1.0,
                    "updated_at": 2.0,
                    "attempts": 1,
                }
            ],
        }
    )

    async def uncertain_inventory(*_args):
        return None

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )

    result = await _test_job_list_execute(session_id)

    assert result.jobs[0].status == "succeeded"
    assert result.jobs[0].exit_code == 0
    assert result.jobs[0].completed_at == 5.0
    assert _load_store_untyped()["jobs"][0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_job_stop_attempts_kill_when_inventory_is_uncertain(monkeypatch):
    row = {
        "job_id": "job_one",
        "kind": "shell",
        "name": "job_one",
        "status": "running",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION1",
        "shell_id": "shell_one",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    store = {"jobs": [row]}
    killed: list[str] = []

    @contextmanager
    def fake_transaction():
        yield store

    async def uncertain_inventory(*_args):
        return None

    async def fake_kill(_config, _store, shell_id: str):
        killed.append(shell_id)
        return SimpleNamespace(
            model_dump=lambda: {"killed": True, "stderr": ""}
        )

    monkeypatch.setattr(
        _TEST_JOB_DEPS,
        "get_store",
        lambda: SimpleNamespace(touch_session=lambda _session_id: None),
    )
    monkeypatch.setattr(job_managed, "managed_job_id_set", lambda *_args: set())
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )
    monkeypatch.setattr(job_shell, "kill_persistent_shell_execute", fake_kill)

    result = await _test_job_stop_execute("SESSION1", "job_one")

    assert result.killed is True
    assert result.job.status == "stopped"
    assert killed == ["shell_one"]


@pytest.mark.asyncio
async def test_job_stop_keeps_job_running_when_kill_is_unconfirmed(monkeypatch):
    row = {
        "job_id": "job_one",
        "kind": "shell",
        "name": "job_one",
        "status": "running",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION1",
        "shell_id": "shell_one",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    store = {"jobs": [row]}
    inventories = iter([{"shell_one"}, None])

    @contextmanager
    def fake_transaction():
        yield store

    async def inventory(*_args):
        return next(inventories)

    async def failed_kill(_config, _store, _shell_id: str):
        return SimpleNamespace(
            model_dump=lambda: {
                "killed": False,
                "stderr": "temporary tmux failure",
            }
        )

    monkeypatch.setattr(
        _TEST_JOB_DEPS,
        "get_store",
        lambda: SimpleNamespace(touch_session=lambda _session_id: None),
    )
    monkeypatch.setattr(job_managed, "managed_job_id_set", lambda *_args: set())
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", inventory
    )
    monkeypatch.setattr(job_shell, "kill_persistent_shell_execute", failed_kill)

    result = await _test_job_stop_execute("SESSION1", "job_one")

    assert result.killed is False
    assert result.job.status == "running"
    assert row["completed_at"] is None
    assert row["error"] == "temporary tmux failure"


@pytest.mark.asyncio
async def test_job_stop_retries_lost_shell_when_inventory_confirms_it_live(
    monkeypatch,
):
    row = {
        "job_id": "job_one",
        "kind": "shell",
        "name": "job_one",
        "status": "lost",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION1",
        "shell_id": "shell_one",
        "created_at": 1.0,
        "updated_at": 1.0,
        "completed_at": 1.0,
        "attempts": 1,
    }
    store = {"jobs": [row]}
    killed: list[str] = []

    @contextmanager
    def fake_transaction():
        yield store

    async def inventory(*_args):
        return {"shell_one"}

    async def fake_kill(_config, _store, shell_id: str):
        killed.append(shell_id)
        return SimpleNamespace(
            model_dump=lambda: {"killed": True, "stderr": ""}
        )

    monkeypatch.setattr(
        _TEST_JOB_DEPS,
        "get_store",
        lambda: SimpleNamespace(touch_session=lambda _session_id: None),
    )
    monkeypatch.setattr(job_managed, "managed_job_id_set", lambda *_args: set())
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", inventory
    )
    monkeypatch.setattr(job_shell, "kill_persistent_shell_execute", fake_kill)

    result = await _test_job_stop_execute("SESSION1", "job_one")

    assert result.killed is True
    assert result.job.status == "stopped"
    assert killed == ["shell_one"]


@pytest.mark.asyncio
async def test_job_stop_confirms_lost_shell_absent_without_kill(monkeypatch):
    row = {
        "job_id": "job_one",
        "kind": "shell",
        "name": "job_one",
        "status": "lost",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION1",
        "shell_id": "shell_one",
        "created_at": 1.0,
        "updated_at": 1.0,
        "completed_at": 1.0,
        "attempts": 1,
    }
    store = {"jobs": [row]}

    @contextmanager
    def fake_transaction():
        yield store

    async def inventory(*_args):
        return set()

    monkeypatch.setattr(
        _TEST_JOB_DEPS,
        "get_store",
        lambda: SimpleNamespace(touch_session=lambda _session_id: None),
    )
    monkeypatch.setattr(job_managed, "managed_job_id_set", lambda *_args: set())
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", inventory
    )
    monkeypatch.setattr(
        job_shell,
        "kill_persistent_shell_execute",
        lambda *_args: pytest.fail("absent shell must not be killed"),
    )

    result = await _test_job_stop_execute("SESSION1", "job_one")

    assert result.killed is False
    assert result.job.status == "stopped"
    assert row["error"] is None


@pytest.mark.asyncio
async def test_job_start_preserves_existing_jobs_when_inventory_is_uncertain(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session(str(tmp_path))
    existing = {
        "job_id": "job_existing",
        "kind": "shell",
        "name": "existing",
        "status": "running",
        "command": "sleep 60",
        "cwd": str(tmp_path),
        "session_id": session_id,
        "shell_id": "shell_existing",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    job_persistence.save_store(
        {"version": job_persistence.JOB_STORE_VERSION, "jobs": [existing]}
    )

    async def uncertain_inventory(*_args):
        return None

    async def fake_start_shell(
        _config,
        _store,
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == session_id
        return StartPersistentShellOutput(
            shell_id="shell_new", name=name, cwd=cwd, command=command
        )

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )
    monkeypatch.setattr(
        job_shell, "start_persistent_shell_execute", fake_start_shell
    )

    started = await _test_job_start_execute(session_id, "echo new")
    rows = _load_store_untyped()["jobs"]

    assert started.status == "running"
    assert {row["job_id"] for row in rows} == {"job_existing", started.job_id}
    assert (
        next(row for row in rows if row["job_id"] == "job_existing")["status"]
        == "running"
    )


@pytest.mark.asyncio
async def test_job_start_does_not_refresh_other_owner_transitions(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session(str(tmp_path))
    other_session_id = _create_session(str(tmp_path))
    unrelated = {
        "job_id": "job_unrelated",
        "kind": "shell",
        "name": "unrelated",
        "status": "starting",
        "command": "sleep 60",
        "cwd": str(tmp_path),
        "session_id": other_session_id,
        "shell_id": "shell_unrelated",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    job_persistence.save_store(
        {"version": job_persistence.JOB_STORE_VERSION, "jobs": [unrelated]}
    )

    async def empty_inventory(*_args):
        return set()

    async def fake_start_shell(
        _config,
        _store,
        cwd: str,
        name: str | None,
        command: str | None,
        *,
        owner_session_id: str | None = None,
    ):
        assert owner_session_id == session_id
        return StartPersistentShellOutput(
            shell_id="shell_new", name=name, cwd=cwd, command=command
        )

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        empty_inventory,
    )
    monkeypatch.setattr(
        job_shell, "start_persistent_shell_execute", fake_start_shell
    )

    await _test_job_start_execute(session_id, "echo new")
    rows = _load_store_untyped()["jobs"]
    unrelated_after = next(
        row for row in rows if row["job_id"] == "job_unrelated"
    )

    assert unrelated_after["status"] == "starting"


@pytest.mark.asyncio
async def test_job_tail_samples_inventory_under_owner_lock(monkeypatch):
    row = {
        "job_id": "job_running",
        "kind": "shell",
        "name": "running",
        "status": "running",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION1",
        "shell_id": "shell_running",
        "log_path": "unused.log",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    store = {"jobs": [row]}
    active_locks: list[str] = []
    inventory_lock_states: list[tuple[str, ...]] = []

    @contextmanager
    def fake_transaction():
        yield store

    @asynccontextmanager
    async def fake_lifecycle_lock(session_id: str):
        active_locks.append(session_id)
        try:
            yield
        finally:
            active_locks.remove(session_id)

    async def live_inventory(*_args):
        inventory_lock_states.append(tuple(active_locks))
        return {"shell_running"}

    monkeypatch.setattr(
        _TEST_JOB_DEPS,
        "get_store",
        lambda: SimpleNamespace(touch_session=lambda _session_id: None),
    )
    monkeypatch.setattr(
        job_shell, "session_lifecycle_lock", fake_lifecycle_lock
    )
    monkeypatch.setattr(job_managed, "managed_job_id_set", lambda *_args: set())
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell.job_status,
        "_read_log_tail",
        lambda *_args, **_kwargs: "ready",
    )
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        live_inventory,
    )

    result = await _test_job_tail_execute("SESSION1", "job_running")

    assert inventory_lock_states == [("SESSION1",)]
    assert result.job.status == "running"


@pytest.mark.asyncio
async def test_job_stop_samples_inventory_under_owner_lock(monkeypatch):
    row = {
        "job_id": "job_running",
        "kind": "shell",
        "name": "running",
        "status": "running",
        "command": "sleep 60",
        "cwd": ".",
        "session_id": "SESSION1",
        "shell_id": "shell_running",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    store = {"jobs": [row]}
    active_locks: list[str] = []
    inventory_lock_states: list[tuple[str, ...]] = []

    @contextmanager
    def fake_transaction():
        yield store

    @asynccontextmanager
    async def fake_lifecycle_lock(session_id: str):
        active_locks.append(session_id)
        try:
            yield
        finally:
            active_locks.remove(session_id)

    async def live_inventory(*_args):
        inventory_lock_states.append(tuple(active_locks))
        return {"shell_running"}

    async def fake_kill(_config, _store, shell_id: str):
        assert active_locks == ["SESSION1"]
        return KillPersistentShellOutput(shell_id=shell_id, killed=True)

    monkeypatch.setattr(
        _TEST_JOB_DEPS,
        "get_store",
        lambda: SimpleNamespace(touch_session=lambda _session_id: None),
    )
    monkeypatch.setattr(
        job_shell, "session_lifecycle_lock", fake_lifecycle_lock
    )
    monkeypatch.setattr(job_managed, "managed_job_id_set", lambda *_args: set())
    monkeypatch.setattr(job_shell, "_store_transaction", fake_transaction)
    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        live_inventory,
    )
    monkeypatch.setattr(job_shell, "kill_persistent_shell_execute", fake_kill)
    monkeypatch.setattr(job_shell, "audit", lambda *_args, **_kwargs: None)

    result = await _test_job_stop_execute("SESSION1", "job_running")

    assert inventory_lock_states == [("SESSION1",)]
    assert result.killed is True
    assert result.job.status == "stopped"


@pytest.mark.asyncio
async def test_job_tail_preserves_running_state_when_inventory_is_uncertain(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session(str(tmp_path))
    row = {
        "job_id": "job_running",
        "kind": "shell",
        "name": "running",
        "status": "running",
        "command": "sleep 60",
        "cwd": str(tmp_path),
        "session_id": session_id,
        "shell_id": "shell_running",
        "log_path": str(tmp_path / "missing.log"),
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    job_persistence.save_store(
        {"version": job_persistence.JOB_STORE_VERSION, "jobs": [row]}
    )

    async def uncertain_inventory(*_args):
        return None

    async def failed_tail(_config, _shell_id: str, _lines: int):
        raise RuntimeError("temporary shell backend failure")

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )
    monkeypatch.setattr(
        job_shell, "read_persistent_shell_output_execute", failed_tail
    )

    result = await _test_job_tail_execute(session_id, "job_running")

    assert result.job.status == "running"
    assert _load_store_untyped()["jobs"][0]["status"] == "running"


@pytest.mark.asyncio
async def test_job_tail_preserves_running_state_when_live_shell_capture_fails(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session(str(tmp_path))
    row = {
        "job_id": "job_running",
        "kind": "shell",
        "name": "running",
        "status": "running",
        "command": "sleep 60",
        "cwd": str(tmp_path),
        "session_id": session_id,
        "shell_id": "shell_running",
        "log_path": str(tmp_path / "missing.log"),
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    job_persistence.save_store(
        {"version": job_persistence.JOB_STORE_VERSION, "jobs": [row]}
    )

    async def live_inventory(*_args):
        return {"shell_running"}

    async def failed_tail(_config, _shell_id: str, _lines: int):
        raise RuntimeError("temporary shell backend failure")

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        live_inventory,
    )
    monkeypatch.setattr(
        job_shell, "read_persistent_shell_output_execute", failed_tail
    )

    result = await _test_job_tail_execute(session_id, "job_running")
    stored = _load_store_untyped()["jobs"][0]

    assert result.job.status == "running"
    assert stored["status"] == "running"
    assert "capture failed" in stored["error"]
    assert "shell_absence_confirmed" not in stored


@pytest.mark.asyncio
async def test_job_tail_marks_lost_only_after_authoritative_shell_absence(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session(str(tmp_path))
    row = {
        "job_id": "job_running",
        "kind": "shell",
        "name": "running",
        "status": "running",
        "command": "sleep 60",
        "cwd": str(tmp_path),
        "session_id": session_id,
        "shell_id": "shell_running",
        "log_path": str(tmp_path / "missing.log"),
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    job_persistence.save_store(
        {"version": job_persistence.JOB_STORE_VERSION, "jobs": [row]}
    )

    async def absent_inventory(*_args):
        return set()

    async def failed_tail(_config, _shell_id: str, _lines: int):
        raise RuntimeError("shell disappeared")

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        absent_inventory,
    )
    monkeypatch.setattr(
        job_shell, "read_persistent_shell_output_execute", failed_tail
    )

    result = await _test_job_tail_execute(session_id, "job_running")
    stored = _load_store_untyped()["jobs"][0]

    assert result.job.status == "lost"
    assert stored["status"] == "lost"
    assert stored["shell_absence_confirmed"] is True


def test_refresh_lost_shell_job_recovers_live_shell():
    row = {
        "job_id": "job_lost",
        "kind": "shell",
        "status": "lost",
        "shell_id": "shell_live",
        "completed_at": 2.0,
        "error": "capture failed",
    }

    refreshed = _test_refresh_job_status(row, {"shell_live"}, now=3.0)

    assert refreshed["status"] == "running"
    assert refreshed["completed_at"] is None
    assert "shell_absence_confirmed" not in refreshed


def test_refresh_lost_shell_job_records_authoritative_absence():
    row = {
        "job_id": "job_lost",
        "kind": "shell",
        "status": "lost",
        "shell_id": "shell_missing",
    }

    refreshed = _test_refresh_job_status(row, set(), now=3.0)

    assert refreshed["status"] == "lost"
    assert refreshed["shell_absence_confirmed"] is True


@pytest.mark.asyncio
async def test_job_retry_rejects_running_job_when_inventory_is_uncertain(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session(str(tmp_path))
    row = {
        "job_id": "job_running",
        "kind": "shell",
        "name": "running",
        "status": "running",
        "command": "sleep 60",
        "cwd": str(tmp_path),
        "session_id": session_id,
        "shell_id": "shell_running",
        "created_at": 1.0,
        "updated_at": 1.0,
        "attempts": 1,
    }
    job_persistence.save_store(
        {"version": job_persistence.JOB_STORE_VERSION, "jobs": [row]}
    )

    async def uncertain_inventory(*_args):
        return None

    monkeypatch.setattr(
        job_shell,
        "authoritative_persistent_shell_ids_execute",
        uncertain_inventory,
    )
    monkeypatch.setattr(
        job_shell,
        "start_persistent_shell_execute",
        lambda *_args, **_kwargs: pytest.fail(
            "running job must not be retried"
        ),
    )

    with pytest.raises(RuntimeError, match="job is still active"):
        await _test_job_retry_execute(session_id, "job_running")

    assert _load_store_untyped()["jobs"][0]["status"] == "running"


def test_managed_job_validation_and_legacy_lost_recovery():
    async def first_handler(context, payload):  # noqa: ARG001
        return payload

    async def second_handler(context, payload):  # noqa: ARG001
        return payload

    with pytest.raises(ValueError, match="must not be empty"):
        job_managed.register_managed_job_handler("   ", first_handler)
    job_managed.register_managed_job_handler(
        "test-managed-validation", first_handler
    )
    job_managed.register_managed_job_handler(
        "test-managed-validation", first_handler
    )
    with pytest.raises(ValueError, match="already registered"):
        job_managed.register_managed_job_handler(
            "test-managed-validation", second_handler
        )

    running = _test_refresh_job_status(
        {
            "job_id": "job_managed_lost",
            "kind": "managed",
            "status": "running",
        },
        set(),
        now=10.0,
    )
    assert running["status"] == "lost"
    assert running["completed_at"] == 10.0
    assert "retry it" in running["error"]

    stopping = _test_refresh_job_status(
        {
            "job_id": "job_managed_stopping",
            "kind": "managed",
            "status": "stopping",
        },
        set(),
        now=11.0,
    )
    assert stopping["status"] == "stopped"
    assert stopping["error"] is None


@pytest.mark.asyncio
async def test_legacy_managed_job_migrates_to_lost_and_can_retry(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()

    async def handler(_context, payload):
        return payload

    job_managed.register_managed_job_handler("test-legacy-retry", handler)
    started = await job_managed.start_managed_job(
        session_id, "test-legacy-retry", {"value": 7}
    )
    current = None
    for _ in range(100):
        await asyncio.sleep(0.01)
        current = (await _test_job_list_execute(session_id)).jobs[0]
        if current.status == "succeeded":
            break
    assert current is not None
    assert current.status == "succeeded"

    with job_recovery.store_transaction() as store:
        row = job_state.find_session_job(store, session_id, started.job_id)
        row.update(
            {
                "status": "running",
                "completed_at": None,
                "exit_code": None,
                "error": None,
            }
        )
        row.pop("managed_lease_version", None)

    migrated = (await _test_job_list_execute(session_id)).jobs[0]
    assert migrated.status == "lost"
    assert "retry it" in str(migrated.error)

    retried = await _test_job_retry_execute(session_id, started.job_id)
    assert retried.attempts == 2
    current = None
    for _ in range(100):
        await asyncio.sleep(0.01)
        current = (await _test_job_list_execute(session_id)).jobs[0]
        if current.status == "succeeded":
            break
    assert current is not None
    assert current.status == "succeeded"
    assert current.result == {"value": 7}


def test_managed_job_lease_registry_rejects_duplicates_and_releases(
    monkeypatch,
):
    events: list[tuple[str, str]] = []

    class FakeLease:
        def __init__(self, job_id: str) -> None:
            self.job_id = job_id

        def acquire(self) -> None:
            events.append(("acquire", self.job_id))

        def release(self) -> None:
            events.append(("release", self.job_id))

    monkeypatch.setattr(job_managed, "ManagedJobLease", FakeLease)
    job_managed.managed_jobs_runtime().leases.clear()

    runtime = job_managed.managed_jobs_runtime()
    lease = runtime.acquire_lease("job_registry")
    assert lease.job_id == "job_registry"
    with pytest.raises(RuntimeError, match="already held"):
        runtime.acquire_lease("job_registry")

    runtime.release_lease("job_registry")
    runtime.release_lease("job_registry")

    assert events == [
        ("acquire", "job_registry"),
        ("release", "job_registry"),
    ]


def test_launch_managed_job_releases_new_lease_when_task_creation_fails(
    monkeypatch,
):
    events: list[tuple[str, str]] = []
    runtime = job_managed.managed_jobs_runtime()

    def fake_acquire(job_id: str):
        events.append(("acquire", job_id))
        lease = job_managed.ManagedJobLease.__new__(job_managed.ManagedJobLease)
        runtime.leases[job_id] = lease
        return lease

    def fake_release(job_id: str) -> None:
        events.append(("release", job_id))
        runtime.leases.pop(job_id, None)

    def fail_create_task(coroutine, *_args, **_kwargs):
        coroutine.close()
        raise RuntimeError("task creation failed")

    runtime.leases.clear()
    monkeypatch.setattr(runtime, "acquire_lease", fake_acquire)
    monkeypatch.setattr(runtime, "release_lease", fake_release)
    monkeypatch.setattr(job_managed.asyncio, "create_task", fail_create_task)

    with pytest.raises(RuntimeError, match="task creation failed"):
        job_managed._launch_managed_job(
            runtime,
            "SESSION1",
            "job_create_failure",
            "test-kind",
            {},
            "job.log",
        )

    assert events == [
        ("acquire", "job_create_failure"),
        ("release", "job_create_failure"),
    ]


@pytest.mark.asyncio
async def test_managed_job_failure_result_bounds_and_launch_rollback(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()

    async def failing_handler(context, payload):  # noqa: ARG001
        raise RuntimeError("managed failure")

    async def oversized_handler(context, payload):  # noqa: ARG001
        return {"value": "x" * (job_state.MANAGED_STATE_MAX_BYTES + 1)}

    async def oversized_progress_handler(context, payload):  # noqa: ARG001
        await context.update_progress(
            value="x" * (job_state.MANAGED_STATE_MAX_BYTES + 1)
        )
        return payload

    async def idle_handler(context, payload):  # noqa: ARG001
        return payload

    job_managed.register_managed_job_handler(
        "test-managed-failure", failing_handler
    )
    failed = await job_managed.start_managed_job(
        session_id, "test-managed-failure", {}
    )
    current = failed
    for _ in range(100):
        await asyncio.sleep(0.01)
        current = (await _test_job_list_execute(session_id)).jobs[0]
        if current.status == "failed":
            break
    assert current.status == "failed"
    assert current.exit_code == 1
    assert current.error == "RuntimeError: managed failure"
    assert (
        "managed failure"
        in (await _test_job_tail_execute(session_id, failed.job_id)).output
    )

    job_managed.register_managed_job_handler(
        "test-managed-oversized-result", oversized_handler
    )
    oversized = await job_managed.start_managed_job(
        session_id, "test-managed-oversized-result", {}
    )
    oversized_row = oversized
    for _ in range(100):
        await asyncio.sleep(0.01)
        rows = await _test_job_list_execute(session_id)
        oversized_row = next(
            row for row in rows.jobs if row.job_id == oversized.job_id
        )
        if oversized_row.status == "failed":
            break
    assert oversized_row.status == "failed"
    assert "managed job result exceeds" in str(oversized_row.error)

    job_managed.register_managed_job_handler(
        "test-managed-oversized-progress", oversized_progress_handler
    )
    progress_job = await job_managed.start_managed_job(
        session_id, "test-managed-oversized-progress", {}
    )
    progress_row = progress_job
    for _ in range(100):
        await asyncio.sleep(0.01)
        rows = await _test_job_list_execute(session_id)
        progress_row = next(
            row for row in rows.jobs if row.job_id == progress_job.job_id
        )
        if progress_row.status == "failed":
            break
    assert progress_row.status == "failed"
    assert "managed job progress exceeds" in str(progress_row.error)

    job_managed.register_managed_job_handler(
        "test-managed-payload-bound", idle_handler
    )
    with pytest.raises(ValueError, match="managed job payload exceeds"):
        await job_managed.start_managed_job(
            session_id,
            "test-managed-payload-bound",
            {"value": "x" * (job_state.MANAGED_STATE_MAX_BYTES + 1)},
        )
    job_managed.register_managed_job_handler(
        "test-managed-launch-failure", idle_handler
    )

    def fail_launch(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("launch failed")

    monkeypatch.setattr(job_managed, "_launch_managed_job", fail_launch)
    before = {
        row.job_id for row in (await _test_job_list_execute(session_id)).jobs
    }
    with pytest.raises(RuntimeError, match="launch failed"):
        await job_managed.start_managed_job(
            session_id, "test-managed-launch-failure", {}
        )
    after = {
        row.job_id for row in (await _test_job_list_execute(session_id)).jobs
    }
    assert after == before
    runtime_files = {
        path.name
        for path in job_persistence.job_runtime_dir().glob("*-attempt-1.log")
    }
    assert runtime_files == {
        f"{failed.job_id}-attempt-1.log",
        f"{oversized.job_id}-attempt-1.log",
        f"{progress_job.job_id}-attempt-1.log",
    }


@pytest.mark.asyncio
async def test_managed_actions_do_not_query_shell_inventory(
    tmp_path, monkeypatch
):
    _configure_job_state(tmp_path, monkeypatch)
    session_id = _create_session()
    release = asyncio.Event()

    async def handler(context, payload):
        await context.update_progress(phase="waiting")
        await release.wait()
        return payload

    job_managed.register_managed_job_handler("test-no-shell-inventory", handler)
    started = await job_managed.start_managed_job(
        session_id,
        "test-no-shell-inventory",
        {"value": 9},
    )

    async def fail_inventory(*_args):
        raise AssertionError("managed action queried shell inventory")

    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", fail_inventory
    )

    tailed = await _test_job_tail_execute(session_id, started.job_id)
    for _ in range(100):
        if tailed.job.progress == {"phase": "waiting"}:
            break
        await asyncio.sleep(0.01)
        tailed = await _test_job_tail_execute(session_id, started.job_id)
    assert tailed.job.progress == {"phase": "waiting"}

    stopped = await _test_job_stop_execute(session_id, started.job_id)
    assert stopped.job.status == "stopped"

    retried = await _test_job_retry_execute(session_id, started.job_id)
    assert retried.status == "running"
    release.set()

    completed = await _test_job_tail_execute(session_id, started.job_id)
    for _ in range(100):
        if completed.job.status == "succeeded":
            break
        await asyncio.sleep(0.01)
        completed = await _test_job_tail_execute(session_id, started.job_id)
    assert completed.job.status == "succeeded"
    assert completed.job.result == {"value": 9}
