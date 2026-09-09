"""Session-aware shell and Python execution facade."""

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from ..schemas.result_models.jobs import JobStartOutput
from ..schemas.result_models.shell import (
    RunPythonCodeOutput,
    ShellExecutionOutput,
)
from ..tool_session.lifecycle import session_lifecycle_lock
from ..tool_session.store import ToolSessionStore
from ..utils.serialization import to_jsonable
from .config import ExecutorConfig
from .shell import (
    _command_with_env,
    _effective_python_executable,
    _shell_join_argv,
    kill_persistent_shell_execute,
    run_shell_command_execute,
    start_persistent_shell_execute,
)
from .temp_file import write_temp_text_file

type JobStarter = Callable[
    [str, str, str, str | None], Awaitable[JobStartOutput]
]


def _as_result_dict(value: Any) -> dict[str, Any]:
    """Return a JSON-compatible result dictionary."""
    data = to_jsonable(value)
    return data if isinstance(data, dict) else {"result": data}


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
                result=_as_result_dict(result),
            )

    if not async_:
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
                result=_as_result_dict(result),
            )

    if job_start is None:
        raise RuntimeError(
            "async shell execution requires executor job service"
        )
    command_with_env = _command_with_env(config, command, env)
    result = await job_start(session_id, command_with_env, cwd, name)
    cwd_text = result.cwd
    return ShellExecutionOutput(
        mode="job",
        command=command,
        cwd=cwd_text,
        result=_as_result_dict(result),
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
) -> RunPythonCodeOutput:
    """Write Python code to a temporary file and execute it through shell modes."""
    session = store.touch_session(session_id)
    resolved_cwd = store.resolve_session_path(session, cwd, must_exist=True)
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
        [_effective_python_executable(config), str(script_path)]
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
    )
    return RunPythonCodeOutput(
        **result.model_dump(), script_path=str(script_path)
    )
