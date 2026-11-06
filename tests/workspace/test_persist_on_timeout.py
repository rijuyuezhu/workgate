"""Opt-in wait-then-detach must use one existing session-owned tracked job."""

from pathlib import Path

import pytest

from tests.support.responses import python_shell_command
from tests.support.sessions import get_test_tool_session_store
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import get_settings
from workgate.executor.path import prune_temp_dir
from workgate.executor.shell_service import ShellService
from workgate.jobs.persistence import job_runtime_dir, prune_store
from workgate.jobs.recovery import store_transaction
from workgate.protocol.ids import new_session_id


def _setup(tmp_path: Path) -> tuple[ShellService, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = get_test_tool_session_store()
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    return ShellService(
        resolve_executor_config(get_settings()), store
    ), session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_fast_command_returns_same_job_and_combined_output(
    tmp_path: Path, exit_code: int
):
    service, session_id = _setup(tmp_path)
    command = python_shell_command(
        f"import sys; print('hello', flush=True); sys.exit({exit_code})"
    )
    result = await service.bash(
        {
            "session_id": session_id,
            "command": command,
            "persist_on_timeout": True,
            "timeout_s": 10,
        }
    )
    assert result.mode == "job"
    assert result.result["timed_out"] is False
    assert result.result["persisted"] is False
    assert result.result["ok"] is (exit_code == 0)
    assert result.result["exit_code"] == exit_code
    assert result.result["job_status"] in {"succeeded", "failed", "exited"}
    assert "hello" in result.result["stdout"]
    assert result.result["stderr"] == ""
    assert result.result["job_id"].startswith("job_")
    assert result.result["session_id"] == session_id
    assert result.result["attempts"] == 1
    assert len((await service.jobs.list(session_id)).jobs) == 1


@pytest.mark.asyncio
async def test_slow_shell_survives_wait_and_is_not_started_twice(
    tmp_path: Path,
):
    service, session_id = _setup(tmp_path)
    marker = tmp_path / "once.txt"
    command = python_shell_command(
        f"from pathlib import Path; import time; "
        f"p=Path({str(marker)!r}); "
        "p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "print('started',flush=True); time.sleep(2); "
        "print('finished',flush=True)"
    )
    result = await service.bash(
        {
            "session_id": session_id,
            "command": command,
            "persist_on_timeout": True,
            "timeout_s": 1,
        }
    )
    assert result.mode == "job"
    assert result.result["job_status"] in {"running", "starting"}
    assert result.result["timed_out"] is True
    assert result.result["persisted"] is True
    assert result.result["ok"] is False
    assert "started" in result.result["stdout"]
    assert marker.read_text() == "x"
    job_id = result.result["job_id"]
    final = await service.jobs.wait(session_id, job_id, 5)
    assert final.job.status == "succeeded"
    assert "finished" in final.output
    assert marker.read_text() == "x"
    assert len((await service.jobs.list(session_id)).jobs) == 1
    assert final.job.attempts == 1


@pytest.mark.asyncio
async def test_python_job_keeps_source_for_pruning_and_retry(tmp_path: Path):
    service, session_id = _setup(tmp_path)
    marker = tmp_path / "python-count.txt"
    code = (
        "from pathlib import Path\nimport time\n"
        f"p=Path({str(marker)!r})\n"
        "p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        "print('python started',flush=True)\n"
        "time.sleep(2)\nprint('python complete',flush=True)\n"
    )
    result = await service.run_python_code(
        {
            "session_id": session_id,
            "code": code,
            "persist_on_timeout": True,
            "timeout_s": 1,
        }
    )
    assert result.mode == "job"
    assert result.result["timed_out"] is True
    assert "python started" in result.result["stdout"]
    script = Path(result.script_path)
    assert script.exists()
    assert script.parent == job_runtime_dir()
    prune_temp_dir(max_files=0, max_bytes=0, directory=service.config.temp_dir)
    assert script.exists()
    job_id = result.result["job_id"]
    final = await service.jobs.wait(session_id, job_id, 5)
    assert final.job.status == "succeeded"
    assert marker.read_text() == "x"
    retry = await service.jobs.retry(session_id, job_id)
    assert retry.job_id == job_id
    final = await service.jobs.wait(session_id, job_id, 5)
    assert final.job.status == "succeeded"
    assert marker.read_text() == "xx"
    assert script.exists()
    with store_transaction() as rows:
        prune_store(rows, max_jobs=0)
    assert not script.exists()


@pytest.mark.asyncio
async def test_persisted_job_can_be_cancelled_with_existing_job_tool(
    tmp_path: Path,
):
    service, session_id = _setup(tmp_path)
    command = python_shell_command(
        "import time; print('ready',flush=True); time.sleep(30)"
    )
    result = await service.bash(
        {
            "session_id": session_id,
            "command": command,
            "persist_on_timeout": True,
            "timeout_s": 1,
        }
    )
    assert result.result["timed_out"] is True
    cancelled = await service.jobs.stop(session_id, result.result["job_id"])
    assert cancelled.killed is True or cancelled.job.status in {
        "stopped",
        "exited",
    }
    polled = await service.jobs.tail(session_id, result.result["job_id"])
    assert polled.job.status in {"stopped", "exited", "failed"}


@pytest.mark.asyncio
async def test_default_timeout_still_terminates_bounded_command(tmp_path: Path):
    service, session_id = _setup(tmp_path)
    result = await service.bash(
        {
            "session_id": session_id,
            "command": python_shell_command("import time; time.sleep(4)"),
            "timeout_s": 1,
        }
    )
    assert result.mode == "command"
    assert result.result["timed_out"] is True
    assert (await service.jobs.list(session_id)).jobs == []


@pytest.mark.asyncio
async def test_flag_with_async_remains_immediate_job(tmp_path: Path):
    service, session_id = _setup(tmp_path)
    result = await service.run_python_code(
        {
            "session_id": session_id,
            "code": "import time; time.sleep(2)",
            "persist_on_timeout": True,
            "async_": True,
            "timeout_s": 1,
        }
    )
    assert result.mode == "job"
    assert result.result["job_id"].startswith("job_")
    assert "timed_out" not in result.result
    assert Path(result.script_path).exists()
    await service.jobs.stop(session_id, result.result["job_id"])


@pytest.mark.asyncio
async def test_persisted_output_is_bounded_without_truncating_durable_log(
    tmp_path: Path,
):
    service, session_id = _setup(tmp_path)
    result = await service.bash(
        {
            "session_id": session_id,
            "command": python_shell_command("print('abcdefghij',flush=True)"),
            "persist_on_timeout": True,
            "timeout_s": 10,
            "max_output_bytes": 5,
        }
    )
    assert result.result["timed_out"] is False
    assert result.result["truncated"] is True
    assert len(result.result["stdout"].encode("utf-8")) <= 5
    full = await service.jobs.tail(session_id, result.result["job_id"])
    assert "abcdefghij" in full.output
    assert full.job.log_truncated is False


@pytest.mark.asyncio
async def test_persisted_job_is_owned_by_its_session(tmp_path: Path):
    service, session_id = _setup(tmp_path)
    second_session = str(new_session_id())
    service.store.create_session(
        session_id=second_session, workdir=tmp_path / "workspace"
    )
    result = await service.bash(
        {
            "session_id": session_id,
            "command": python_shell_command("print('private')"),
            "persist_on_timeout": True,
            "timeout_s": 10,
        }
    )
    with pytest.raises(KeyError, match="job not found in session"):
        await service.jobs.tail(second_session, result.result["job_id"])
    assert (
        "private"
        in (await service.jobs.tail(session_id, result.result["job_id"])).output
    )


@pytest.mark.asyncio
async def test_invalid_persisted_python_timeout_creates_no_script_or_job(
    tmp_path: Path,
):
    service, session_id = _setup(tmp_path)
    existing = set(job_runtime_dir().glob("python-script-*.py"))
    with pytest.raises(ValueError, match="timeout_s"):
        await service.run_python_code(
            {
                "session_id": session_id,
                "code": "print('not executed')",
                "persist_on_timeout": True,
                "timeout_s": service.config.run_shell_max_timeout_s + 1,
            }
        )
    assert set(job_runtime_dir().glob("python-script-*.py")) == existing
    assert (await service.jobs.list(session_id)).jobs == []


@pytest.mark.asyncio
async def test_tool_registry_exposes_opt_in_flag_for_both_tools() -> None:
    from workgate.control.mcp.app import build_mcp

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    for name in ("bash", "run_python_code"):
        spec = tools[name].input_schema
        assert "persist_on_timeout" in spec["properties"]
        assert spec["properties"]["persist_on_timeout"]["default"] is False
        assert "persist_on_timeout" not in spec.get("required", [])


def test_durable_script_cleanup_ignores_unowned_metadata(tmp_path: Path):
    from workgate.jobs.persistence import job_runtime_dir

    unrelated = tmp_path / "do-not-delete.py"
    unrelated.write_text("private content", encoding="utf-8")
    with store_transaction() as rows:
        rows["jobs"].append(
            {
                "job_id": "job_unowned-test",
                "kind": "shell",
                "session_id": "test",
                "status": "succeeded",
                "script_path": str(unrelated),
            }
        )
        prune_store(rows, max_jobs=0)
    assert unrelated.read_text() == "private content"
    assert job_runtime_dir().is_dir()
