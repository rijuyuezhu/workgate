import asyncio

import pytest

import workgate.ops.bash as shell_ops
import workgate.ops.session as session_ops
from tests.helpers import mcp_structured, python_shell_command
from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp
from workgate.schemas.result_models.jobs import JobStartOutput
from workgate.schemas.result_models.shell import (
    RunShellCommandOutput,
    StartPersistentShellOutput,
)
from workgate.tool_session.store import get_tool_session_store


def _create_session(workdir: str = ".") -> str:
    store = get_tool_session_store()
    store.clear()
    return store.create_session(workdir=workdir).session_id


@pytest.mark.asyncio
async def test_shell_execution_runs_bounded_command_in_session_workdir(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    session_dir = tmp_path / "project"
    session_dir.mkdir()
    session_id = _create_session("project")
    command = python_shell_command(
        "import os; print(os.environ['FOO'] + ':' + os.getcwd(), end='')"
    )

    result = await shell_ops.bash_execute(
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
    session_id = _create_session()
    command_entered = asyncio.Event()
    release_command = asyncio.Event()
    teardown_entered = asyncio.Event()

    async def fake_run(command, cwd, timeout_s, max_output_bytes, env):
        _ = (timeout_s, max_output_bytes, env)
        command_entered.set()
        await release_command.wait()
        return RunShellCommandOutput(
            ok=True,
            exit_code=0,
            duration_ms=1,
            cwd=cwd,
            command=command,
        )

    async def fake_end(session_id_arg: str, *, force: bool = False):
        _ = (session_id_arg, force)
        teardown_entered.set()
        return object()

    monkeypatch.setattr(shell_ops, "run_shell_command_execute", fake_run)
    monkeypatch.setattr(session_ops, "_session_end_execute_unlocked", fake_end)

    command_task = asyncio.create_task(
        shell_ops.bash_execute(session_id, "long-running")
    )
    await command_entered.wait()
    teardown_task = asyncio.create_task(
        session_ops.session_end_execute(session_id)
    )
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
    session_id = _create_session()
    command_entered = asyncio.Event()
    release_command = asyncio.Event()
    teardown_entered = asyncio.Event()

    async def fake_run(command, cwd, timeout_s, max_output_bytes, env):
        _ = (timeout_s, max_output_bytes, env)
        command_entered.set()
        await release_command.wait()
        return RunShellCommandOutput(
            ok=True,
            exit_code=0,
            duration_ms=1,
            cwd=cwd,
            command=command,
        )

    async def fake_end(session_id_arg: str, *, force: bool = False):
        _ = (session_id_arg, force)
        teardown_entered.set()
        return object()

    async def fake_temp_file(*_args, **_kwargs):
        return tmp_path / "script.py"

    monkeypatch.setattr(shell_ops, "run_shell_command_execute", fake_run)
    monkeypatch.setattr(shell_ops, "write_temp_text_file", fake_temp_file)
    monkeypatch.setattr(session_ops, "_session_end_execute_unlocked", fake_end)

    command_task = asyncio.create_task(
        shell_ops.run_python_code_execute(session_id, "print('hello')")
    )
    await command_entered.wait()
    teardown_task = asyncio.create_task(
        session_ops.session_end_execute(session_id)
    )
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
    session_id = _create_session("project")

    with pytest.raises(ValueError, match="Path escapes session workdir"):
        await shell_ops.bash_execute(session_id, "pwd", cwd="../other")


@pytest.mark.asyncio
async def test_shell_execution_routes_async_to_session_job(monkeypatch):
    calls = []

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

    class FakeStore:
        def touch_session(self, session_id):
            from workgate.tool_session.store import AgentSession

            return AgentSession(
                session_id=session_id,
                target="local",
                workdir="/tmp/project",
                machine=None,
                worker_session_id=None,
                created_at=1.0,
                updated_at=1.0,
            )

    monkeypatch.setattr(
        "workgate.jobs.runtime.job_start_execute", fake_job_start
    )
    monkeypatch.setattr(
        shell_ops, "get_tool_session_store", lambda: FakeStore()
    )
    monkeypatch.setattr(
        shell_ops,
        "resolve_session_path",
        lambda session, cwd, must_exist=False: "/tmp/project/app",
    )

    result = await shell_ops.bash_execute(
        "ABC12345", "npm test", cwd="app", async_=True, name="tests"
    )

    assert result.mode == "job"
    assert result.result["job_id"] == "job_123"
    assert result.result["session_id"] == "ABC12345"
    assert "backend" not in result.result
    assert calls == [("ABC12345", "npm test", "/tmp/project/app", "tests")]


@pytest.mark.asyncio
async def test_shell_execution_routes_pty_to_persistent_shell(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    session_id = _create_session()
    calls = []

    async def fake_start_shell(
        cwd=".", name=None, command=None, *, owner_session_id=None
    ):
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
        session_id, "python -i", cwd=".", pty=True, name="server"
    )

    assert result.mode == "pty"
    assert result.result["shell_id"] == "shell-1"
    assert calls == [(str(tmp_path), "server", "python -i", session_id)]
    assert get_tool_session_store().require_session(
        session_id
    ).persistent_shell_ids == ("shell-1",)


@pytest.mark.asyncio
async def test_pty_registration_failure_rolls_back_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    session_id = _create_session()
    killed: list[str] = []

    async def fake_start_shell(*_args, **_kwargs):
        return StartPersistentShellOutput(shell_id="shell-1", backend="tmux")

    async def fake_kill(shell_id: str):
        killed.append(shell_id)

    store = get_tool_session_store()
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
        await shell_ops.bash_execute(session_id, "python -i", pty=True)

    assert killed == ["shell-1"]


@pytest.mark.asyncio
async def test_shell_execution_is_exposed_in_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    get_tool_session_store().clear()

    mcp = build_mcp()
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
