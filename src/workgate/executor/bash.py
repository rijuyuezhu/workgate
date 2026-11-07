"""Session-aware shell and Python execution facade."""

import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..config.executor import ExecutorConfig
from ..jobs.persistence import TERMINAL_STATUSES
from ..schemas.result_models.jobs import JobStartOutput, JobTailOutput
from ..schemas.result_models.shell import (
    RunPythonCodeOutput,
    ShellExecutionOutput,
)
from ..utils.serialization import to_jsonable
from .shell import (
    _command_with_env,
    _effective_output_limit,
    _effective_python_executable,
    _effective_shell_executable,
    _shell_join_argv,
    kill_persistent_shell_execute,
    run_shell_command_execute,
    run_shell_command_timeout,
    start_persistent_shell_execute,
)
from .temp_file import write_durable_python_script, write_temp_text_file
from .tool_session.lifecycle import session_lifecycle_lock
from .tool_session.store import ToolSessionStore

type JobStarter = Callable[..., Awaitable[JobStartOutput]]
type JobWaiter = Callable[[str, str, float], Awaitable[JobTailOutput]]


def _as_result_dict(
    value: Any,
    *,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Return a JSON-compatible result dictionary."""
    data = to_jsonable(value)
    result = data if isinstance(data, dict) else {"result": data}
    if cwd is not None and "cwd" in result:
        result["cwd"] = cwd
    return result


async def bash_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    session_id: str,
    command: str,
    cwd: str = ".",
    timeout_s: int | None = None,
    max_output_bytes: int | None = None,
    env: dict[str, str] | None = None,
    async_: bool = False,
    pty: bool = False,
    name: str | None = None,
    *,
    job_start: JobStarter | None = None,
    forbidden_shell_ids: frozenset[str] = frozenset(),
    persist_on_timeout: bool = False,
    job_wait: JobWaiter | None = None,
    job_script_path: str | None = None,
) -> ShellExecutionOutput:
    """Run a shell command under explicit executor-owned session authority."""
    if pty:
        async with session_lifecycle_lock(session_id):
            session = store.touch_session(session_id)
            resolved_cwd = store.resolve_session_path(
                session, cwd, must_exist=True
            )
            cwd_text = str(resolved_cwd)
            command_with_env = _command_with_env(config, command, env)
            result = await start_persistent_shell_execute(
                config,
                store,
                cwd_text,
                name,
                command_with_env,
                owner_session_id=session_id,
                forbidden_shell_ids=forbidden_shell_ids,
            )
            try:
                store.register_persistent_shell(session_id, result.shell_id)
            except BaseException:
                with contextlib.suppress(Exception):
                    await kill_persistent_shell_execute(
                        config, store, result.shell_id
                    )
                raise
            return ShellExecutionOutput(
                mode="pty",
                command=command,
                cwd=cwd_text,
                result=_as_result_dict(result, cwd=cwd_text),
            )

    if not async_ and not persist_on_timeout:
        async with session_lifecycle_lock(session_id):
            session = store.touch_session(session_id)
            resolved_cwd = store.resolve_session_path(
                session, cwd, must_exist=True
            )
            cwd_text = str(resolved_cwd)
            result = await run_shell_command_execute(
                config,
                command,
                cwd_text,
                timeout_s,
                max_output_bytes,
                env,
            )
            return ShellExecutionOutput(
                mode="command",
                command=command,
                cwd=cwd_text,
                result=_as_result_dict(result, cwd=cwd_text),
            )

    if job_start is None:
        raise RuntimeError(
            "async shell execution requires executor job service"
        )
    if persist_on_timeout and not async_ and job_wait is None:
        raise RuntimeError("persist_on_timeout requires the tracked job waiter")
    timeout = (
        run_shell_command_timeout(config, timeout_s)
        if persist_on_timeout and not async_
        else None
    )
    started = time.monotonic()
    command_with_env = _command_with_env(config, command, env)
    if job_script_path is None:
        result = await job_start(session_id, command_with_env, cwd, name)
    else:
        result = await job_start(
            session_id, command_with_env, cwd, name, script_path=job_script_path
        )
    cwd_text = result.cwd
    payload = _as_result_dict(result)
    if timeout is not None:
        assert job_wait is not None
        snapshot = await job_wait(
            session_id,
            result.job_id,
            max(0.0, timeout - (time.monotonic() - started)),
        )
        job = snapshot.job
        finished = job.status in TERMINAL_STATUSES
        limit = _effective_output_limit(config, max_output_bytes)
        raw = snapshot.output.encode("utf-8")
        captured = raw[-limit:].decode("utf-8", errors="ignore")
        payload.update(
            _as_result_dict(job)
            | {
                "job_status": job.status,
                "timed_out": not finished,
                "persisted": not finished,
                "ok": finished and job.exit_code == 0,
                "exit_code": job.exit_code if finished else None,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "stdout": captured,
                "stderr": "",  # Tracked jobs combine stdout and stderr.
                "truncated": job.log_truncated or len(raw) > limit,
            }
        )
    return ShellExecutionOutput(
        mode="job",
        command=command,
        cwd=cwd_text,
        result=payload,
    )


async def run_python_code_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    session_id: str,
    code: str,
    cwd: str = ".",
    timeout_s: int | None = None,
    max_output_bytes: int | None = None,
    env: dict[str, str] | None = None,
    async_: bool = False,
    pty: bool = False,
    name: str | None = None,
    *,
    job_start: JobStarter | None = None,
    forbidden_shell_ids: frozenset[str] = frozenset(),
    python_executable: str | None = None,
    persist_on_timeout: bool = False,
    job_wait: JobWaiter | None = None,
) -> RunPythonCodeOutput:
    """Write Python code to a temporary file and execute it through shell modes."""
    session = store.touch_session(session_id)
    resolved_cwd = store.resolve_session_path(session, cwd, must_exist=True)
    if persist_on_timeout and not async_ and not pty:
        run_shell_command_timeout(config, timeout_s)
    if not pty and (persist_on_timeout or async_):
        # A tracked job may outlive normal scratch-file pruning or be retried.
        script_path = write_durable_python_script(
            code, max_input_bytes=config.max_file_write_bytes
        )
    else:
        script_path = await write_temp_text_file(
            "Python script",
            code,
            "script",
            "py",
            max_input_bytes=config.max_file_write_bytes,
            max_tmp_files=config.max_tmp_files,
            max_tmp_bytes=config.max_tmp_bytes,
            temp_directory=config.temp_dir,
        )
    command = _shell_join_argv(
        [
            python_executable or _effective_python_executable(config),
            str(script_path),
        ],
        shell=_effective_shell_executable(config),
    )
    result = await bash_execute(
        config,
        store,
        session_id,
        command,
        str(resolved_cwd),
        timeout_s,
        max_output_bytes,
        env,
        async_,
        pty,
        name,
        job_start=job_start,
        forbidden_shell_ids=forbidden_shell_ids,
        persist_on_timeout=persist_on_timeout,
        job_wait=job_wait,
        job_script_path=str(script_path)
        if not pty and (persist_on_timeout or async_)
        else None,
    )
    return RunPythonCodeOutput(
        **result.model_dump(), script_path=str(script_path)
    )
