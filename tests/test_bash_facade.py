import asyncio
from pathlib import Path

import pytest

import workgate.executor.bash as shell_ops
from tests.helpers import (
    build_paired_control_harness,
    mcp_structured,
    python_shell_command,
)
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.mcp.app import build_mcp
from workgate.executor.config import ExecutorConfig, resolve_executor_config
from workgate.executor.tool_session.lifecycle import session_lifecycle_lock
from workgate.executor.tool_session.store import (
    ToolSessionStore,
    get_tool_session_store,
)
from workgate.schemas.result_models.jobs import JobStartOutput
from workgate.schemas.result_models.shell import (
    RunShellCommandOutput,
    StartPersistentShellOutput,
)


def _create_session(
    workdir: str = ".",
) -> tuple[ExecutorConfig, ToolSessionStore, str]:
    config = resolve_executor_config(get_settings())
    store = get_tool_session_store()
    store.clear()
    session_id = "sess_0000000000000000000001"
    target = Path(workdir)
    if not target.is_absolute():
        target = config.workspace_root / target
    store.create_session(session_id=session_id, workdir=target.resolve())
    return config, store, session_id


@pytest.mark.asyncio
async def test_shell_execution_runs_bounded_command_in_session_workdir(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    session_dir = tmp_path / "project"
    session_dir.mkdir()
    config, store, session_id = _create_session("project")
    command = python_shell_command(
        "import os; print(os.environ['FOO'] + ':' + os.getcwd(), end='')"
    )

    result = await shell_ops.bash_execute(
        config,
        store,
        session_id,
        command,
        cwd=".",
        env={"FOO": "hello"},
    )

    assert result.mode == "command"
    assert result.command == command
    assert result.cwd == str(session_dir)
    assert result.result["ok"] is True
    assert result.result["stdout"] == f"hello:{session_dir}"


@pytest.mark.asyncio
async def test_foreground_shell_blocks_session_teardown(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    config, store, session_id = _create_session()
    command_entered = asyncio.Event()
    release_command = asyncio.Event()
    teardown_entered = asyncio.Event()

    async def fake_run(
        config_arg, command, cwd, timeout_s, max_output_bytes, env
    ):
        _ = (config_arg, timeout_s, max_output_bytes, env)
        command_entered.set()
        await release_command.wait()
        return RunShellCommandOutput(
            ok=True,
            exit_code=0,
            duration_ms=1,
            cwd=cwd,
            command=command,
        )

    async def teardown() -> None:
        async with session_lifecycle_lock(session_id):
            teardown_entered.set()

    monkeypatch.setattr(shell_ops, "run_shell_command_execute", fake_run)

    command_task = asyncio.create_task(
        shell_ops.bash_execute(config, store, session_id, "long-running")
    )
    await command_entered.wait()
    teardown_task = asyncio.create_task(teardown())
    await asyncio.sleep(0.05)
    assert not teardown_entered.is_set()

    release_command.set()
    await command_task
    await teardown_task
    assert teardown_entered.is_set()


@pytest.mark.asyncio
async def test_foreground_python_blocks_session_teardown(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    config, store, session_id = _create_session()
    command_entered = asyncio.Event()
    release_command = asyncio.Event()
    teardown_entered = asyncio.Event()

    async def fake_run(
        config_arg, command, cwd, timeout_s, max_output_bytes, env
    ):
        _ = (config_arg, timeout_s, max_output_bytes, env)
        command_entered.set()
        await release_command.wait()
        return RunShellCommandOutput(
            ok=True,
            exit_code=0,
            duration_ms=1,
            cwd=cwd,
            command=command,
        )

    async def teardown() -> None:
        async with session_lifecycle_lock(session_id):
            teardown_entered.set()

    async def fake_temp_file(*_args, **_kwargs):
        return tmp_path / "script.py"

    monkeypatch.setattr(shell_ops, "run_shell_command_execute", fake_run)
    monkeypatch.setattr(shell_ops, "write_temp_text_file", fake_temp_file)

    command_task = asyncio.create_task(
        shell_ops.run_python_code_execute(
            config, store, session_id, "print('hello')"
        )
    )
    await command_entered.wait()
    teardown_task = asyncio.create_task(teardown())
    await asyncio.sleep(0.05)
    assert not teardown_entered.is_set()

    release_command.set()
    await command_task
    await teardown_task
    assert teardown_entered.is_set()


@pytest.mark.asyncio
async def test_shell_execution_rejects_cwd_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "project").mkdir()
    (tmp_path / "other").mkdir()
    config, store, session_id = _create_session("project")

    with pytest.raises(ValueError, match="Path escapes session workdir"):
        await shell_ops.bash_execute(
            config, store, session_id, "pwd", cwd="../other"
        )


@pytest.mark.asyncio
async def test_shell_execution_routes_async_to_session_job(monkeypatch):
    calls = []
    config = resolve_executor_config(get_settings())
    store = get_tool_session_store()

    async def fake_job_start(session_id, command, cwd=".", name=None):
        calls.append((session_id, command, cwd, name))
        return JobStartOutput.model_validate(
            {
                "job_id": "job_123",
                "name": name,
                "status": "running",
                "command": command,
                "cwd": cwd,
                "session_id": session_id,
                "created_at": 1.0,
                "updated_at": 1.0,
                "last_started_at": 1.0,
                "attempts": 1,
            }
        )

    result = await shell_ops.bash_execute(
        config,
        store,
        "ABC12345",
        "npm test",
        cwd="app",
        async_=True,
        name="tests",
        job_start=fake_job_start,
    )

    assert result.mode == "job"
    assert result.result["job_id"] == "job_123"
    assert result.result["session_id"] == "ABC12345"
    assert "backend" not in result.result
    assert calls == [("ABC12345", "npm test", "app", "tests")]


@pytest.mark.asyncio
async def test_shell_execution_routes_pty_to_persistent_shell(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    config, store, session_id = _create_session()
    calls = []

    async def fake_start_shell(
        config_arg,
        store_arg,
        cwd=".",
        name=None,
        command=None,
        *,
        owner_session_id=None,
    ):
        assert config_arg is config
        assert store_arg is store
        calls.append((cwd, name, command, owner_session_id))
        return StartPersistentShellOutput.model_validate(
            {
                "shell_id": "shell-1",
                "name": "server",
                "cwd": cwd,
                "backend": "tmux",
                "started": True,
            }
        )

    monkeypatch.setattr(
        shell_ops, "start_persistent_shell_execute", fake_start_shell
    )

    result = await shell_ops.bash_execute(
        config,
        store,
        session_id,
        "python -i",
        cwd=".",
        pty=True,
        name="server",
    )

    assert result.mode == "pty"
    assert result.result["shell_id"] == "shell-1"
    assert calls == [(str(tmp_path), "server", "python -i", session_id)]
    assert store.require_session(session_id).persistent_shell_ids == (
        "shell-1",
    )


@pytest.mark.asyncio
async def test_pty_registration_failure_rolls_back_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    config, store, session_id = _create_session()
    killed: list[str] = []

    async def fake_start_shell(*_args, **_kwargs):
        return StartPersistentShellOutput(shell_id="shell-1", backend="tmux")

    async def fake_kill(config_arg, store_arg, shell_id: str):
        assert config_arg is config
        assert store_arg is store
        killed.append(shell_id)

    monkeypatch.setattr(
        shell_ops, "start_persistent_shell_execute", fake_start_shell
    )
    monkeypatch.setattr(shell_ops, "kill_persistent_shell_execute", fake_kill)
    monkeypatch.setattr(
        store,
        "register_persistent_shell",
        lambda _session_id, _shell_id: (_ for _ in ()).throw(
            RuntimeError("metadata write failed")
        ),
    )

    with pytest.raises(RuntimeError, match="metadata write failed"):
        await shell_ops.bash_execute(
            config, store, session_id, "python -i", pty=True
        )

    assert killed == ["shell-1"]


@pytest.mark.asyncio
async def test_shell_execution_is_exposed_in_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    get_tool_session_store().clear()

    mcp = build_mcp(
        runtime=build_paired_control_harness(get_settings()).control
    )
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )
    payload = mcp_structured(
        await mcp.call_tool(
            "bash",
            {
                "session_id": session["session_id"],
                "command": python_shell_command("print('hi', end='')"),
            },
        )
    )

    assert payload["mode"] == "command"
    assert payload["cwd"] == str(tmp_path)
    assert payload["result"]["stdout"] == "hi"
