import pytest

import workgate.ops.bash as shell_ops
import workgate.ops.files as file_ops
import workgate.ops.read as read_ops
import workgate.ops.remote as remote_ops
import workgate.ops.search as search_ops
import workgate.ops.session as session_ops
import workgate.remote.service as remote_service
import workgate.tools.ops.jobs as job_ops
from tests.helpers import mcp_structured
from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp
from workgate.tool_session.store import get_tool_session_store


@pytest.mark.asyncio
async def test_remote_admin_lists(monkeypatch):
    monkeypatch.setattr(
        remote_ops,
        "list_remote_machines",
        lambda: {"machines": [], "counts": {"total": 0}},
    )

    result = await remote_ops.remote_admin_execute("list", {})

    assert result.action == "list"
    assert result.data == {"machines": [], "counts": {"total": 0}}


@pytest.mark.asyncio
async def test_remote_admin_returns_reconnect_command(monkeypatch):
    monkeypatch.setattr(
        remote_ops,
        "remote_reconnect_command",
        lambda machine: {
            "machine": machine,
            "profile_id": "p_abcdefgh",
            "command": "/state/run p_abcdefgh",
        },
    )

    result = await remote_ops.remote_admin_execute(
        "reconnect_command", {"machine": "worker-a"}
    )

    assert result.action == "reconnect_command"
    assert result.data == {
        "machine": "worker-a",
        "profile_id": "p_abcdefgh",
        "command": "/state/run p_abcdefgh",
    }


def test_remote_service_delegates_reconnect_and_rename(monkeypatch):
    calls = []

    class Manager:
        def reconnect_command(self, machine):
            calls.append(("reconnect", machine))
            return {"machine": machine, "command": "run profile"}

        def rename(self, machine, new_name):
            calls.append(("rename", machine, new_name))
            return {"old_name": machine, "new_name": new_name}

    manager = Manager()
    monkeypatch.setattr(remote_service, "remote_manager", lambda: manager)

    assert remote_service.remote_reconnect_command("edge-a") == {
        "machine": "edge-a",
        "command": "run profile",
    }
    assert remote_service.rename_remote_machine("edge-a", "edge-b") == {
        "old_name": "edge-a",
        "new_name": "edge-b",
    }
    assert calls == [
        ("reconnect", "edge-a"),
        ("rename", "edge-a", "edge-b"),
    ]


@pytest.mark.asyncio
async def test_remote_admin_is_exposed_in_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "true")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    monkeypatch.setattr(
        remote_ops,
        "list_remote_machines",
        lambda: {"machines": [], "counts": {"total": 0}},
    )

    result = mcp_structured(
        await build_mcp().call_tool(
            "remote_admin", {"action": "list", "args": {}}
        )
    )

    assert result == {
        "action": "list",
        "data": {"machines": [], "counts": {"total": 0}},
    }


@pytest.mark.asyncio
async def test_remote_reconnect_command_is_exposed_in_mcp(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "true")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    monkeypatch.setattr(
        remote_ops,
        "remote_reconnect_command",
        lambda machine: {
            "machine": machine,
            "profile_id": "p_abcdefgh",
            "command": "/state/run p_abcdefgh",
        },
    )

    result = mcp_structured(
        await build_mcp().call_tool(
            "remote_admin",
            {
                "action": "reconnect_command",
                "args": {"machine": "worker-a"},
            },
        )
    )

    assert result["action"] == "reconnect_command"
    assert result["data"]["profile_id"] == "p_abcdefgh"
    assert result["data"]["command"] == "/state/run p_abcdefgh"


def _remote_read_payload(worker_session_id="WORKER12"):
    return {
        "kind": "file",
        "path": "demo.txt",
        "raw": False,
        "content": "1|hello",
        "file": {
            "path": "demo.txt",
            "bytes": 6,
            "bytes_read": 6,
            "truncated_bytes": 0,
            "total_lines": 1,
            "start_line": 1,
            "end_line": 1,
            "line_count": 1,
            "lines": [{"line": 1, "text": "hello"}],
            "numbered_content": "1|hello",
            "session_id": worker_session_id,
            "snapshot_id": "snap1",
            "file_sha256": "abc",
            "seen_ranges": [{"start": 1, "end": 1}],
            "truncated": False,
            "content": "hello",
        },
    }


def _remote_search_payload(worker_session_id="WORKER12"):
    return {
        "ok": True,
        "matches": [
            {
                "path": "demo.txt",
                "line": 1,
                "column": 1,
                "text": "hello",
                "numbered_line": "1|hello",
                "session_id": worker_session_id,
                "snapshot_id": "snap-search",
                "file_sha256": "abc",
                "seen_range": {"start": 1, "end": 1},
            }
        ],
        "count": 1,
        "truncated": False,
        "stderr": "",
        "numbered_content": "demo.txt\n1|hello",
    }


def _remote_edit_payload(worker_session_id="WORKER12"):
    payload = _remote_read_payload(worker_session_id)["file"]
    payload.update(
        {
            "numbered_content": "1|changed",
            "content": "changed",
            "lines": [{"line": 1, "text": "changed"}],
            "snapshot_id": "snap-edit",
        }
    )
    return {
        "path": "demo.txt",
        "start_line": 1,
        "end_line": 1,
        "replacement_line_count": 1,
        "diff": "--- demo.txt\n+++ demo.txt\n",
        "context": payload,
    }


def _remote_job_payload(worker_session_id="WORKER12"):
    return {
        "operation": "list",
        "jobs": [
            {
                "job_id": "job_1",
                "name": "job_1",
                "status": "running",
                "command": "sleep 1",
                "cwd": "/remote/project",
                "session_id": worker_session_id,
                "created_at": 1.0,
                "updated_at": 1.0,
                "last_started_at": 1.0,
                "attempts": 1,
            }
        ],
        "counts": {"running": 1},
        "outputs": [],
        "cancelled": [],
        "retried": [],
        "message": None,
    }


@pytest.mark.asyncio
async def test_session_start_creates_remote_control_session(
    monkeypatch, tmp_path
):
    remote_root = tmp_path / "remote"
    remote_project = remote_root / "project"
    remote_project.mkdir(parents=True)
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(remote_root))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    calls = []
    worker_session_ids = []

    async def fake_start_worker_session(*, machine, workdir, label=None):
        calls.append((machine, workdir, label))
        worker = session_ops._start_local_session(
            workdir=str(remote_project), machine=None, label=label
        )
        worker_session_ids.append(worker.session_id)
        return worker.model_dump(mode="json")

    monkeypatch.setattr(
        session_ops, "start_worker_session", fake_start_worker_session
    )

    result = await session_ops.session_start_execute(
        "project", target="remote", machine="worker-a", label="demo"
    )
    record = store.require_session(result.session_id)

    assert result.target == "remote"
    assert result.machine == "worker-a"
    assert result.workdir == str(remote_project)
    assert result.workspace_root == str(remote_root)
    assert result.environment.workspace.workspace_root == str(remote_root)
    assert result.environment.workspace.target == "remote"
    assert result.environment.workspace.machine == "worker-a"
    assert "worker_session_id" not in result.model_dump()
    assert record.worker_session_id == worker_session_ids[0]
    assert record.worker_session_id != result.session_id
    assert calls == [("worker-a", "project", "demo")]


@pytest.mark.asyncio
async def test_session_start_remote_requires_machine():
    with pytest.raises(ValueError, match="machine is required"):
        await session_ops.session_start_execute(".", target="remote")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "match"),
    [
        (
            ValueError("unknown remote machine: worker-a"),
            "unknown remote machine",
        ),
        (RuntimeError("remote machine is offline: worker-a"), "offline"),
    ],
)
async def test_session_start_remote_surfaces_missing_or_offline_worker(
    monkeypatch, exc, match
):
    async def fake_start_worker_session(*, machine, workdir, label=None):
        raise exc

    monkeypatch.setattr(
        session_ops, "start_worker_session", fake_start_worker_session
    )

    with pytest.raises(type(exc), match=match):
        await session_ops.session_start_execute(
            "/remote/project", target="remote", machine="worker-a"
        )


@pytest.mark.asyncio
async def test_remote_session_dispatches_read_search_edit_bash_job(monkeypatch):
    store = get_tool_session_store()
    store.clear()
    control = store.create_session(
        target="remote",
        workdir="/remote/project",
        machine="worker-a",
        worker_session_id="WORKER12",
    )
    calls = []

    async def fake_call(machine, tool, args, timeout_s=None):
        calls.append((machine, tool, args, timeout_s))
        data_by_tool = {
            "read": _remote_read_payload(),
            "search": _remote_search_payload(),
            "edit_lines": _remote_edit_payload(),
            "bash": {
                "mode": "command",
                "command": "printf hi",
                "cwd": "/remote/project",
                "result": {"ok": True, "stdout": "hi", "stderr": ""},
            },
            "job": _remote_job_payload(),
        }
        return {"ok": True, "data": data_by_tool[tool]}

    monkeypatch.setattr(
        "workgate.ops.utils.remote_session.call_remote_worker_tool",
        fake_call,
    )

    read_result = await read_ops.read_execute("demo.txt:1", control.session_id)
    search_result = await search_ops.search_execute(
        "hello", session_id=control.session_id, regex=False, gitignore=False
    )
    edit_result = await file_ops.edit_lines_dispatch_execute(
        "demo.txt", 1, 1, "changed", "snap1", control.session_id
    )
    bash_result = await shell_ops.bash_execute(
        control.session_id, "printf hi", timeout_s=5
    )
    job_result = await job_ops.job_execute(control.session_id)

    assert read_result.file is not None
    assert read_result.file.session_id == control.session_id
    assert search_result.matches[0].session_id == control.session_id
    assert edit_result.context.session_id == control.session_id
    assert bash_result.result["stdout"] == "hi"
    assert job_result.jobs[0].session_id == control.session_id
    assert calls == [
        (
            "worker-a",
            "read",
            {"path": "demo.txt:1", "session_id": "WORKER12"},
            None,
        ),
        (
            "worker-a",
            "search",
            {
                "pattern": "hello",
                "paths": None,
                "regex": False,
                "case_sensitive": True,
                "max_results": None,
                "skip": 0,
                "gitignore": False,
                "session_id": "WORKER12",
            },
            None,
        ),
        (
            "worker-a",
            "edit_lines",
            {
                "path": "demo.txt",
                "start_line": 1,
                "end_line": 1,
                "replacement": "changed",
                "snapshot_id": "snap1",
                "session_id": "WORKER12",
            },
            None,
        ),
        (
            "worker-a",
            "bash",
            {
                "command": "printf hi",
                "cwd": ".",
                "timeout_s": 5,
                "max_output_bytes": None,
                "env": None,
                "async_": False,
                "pty": False,
                "name": None,
                "session_id": "WORKER12",
            },
            5,
        ),
        (
            "worker-a",
            "job",
            {
                "list_jobs": False,
                "poll": None,
                "cancel": None,
                "retry": None,
                "include_finished": True,
                "lines": 200,
                "session_id": "WORKER12",
            },
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_remote_session_rejects_pty_bash(monkeypatch):
    store = get_tool_session_store()
    store.clear()
    control = store.create_session(
        target="remote",
        workdir="/remote/project",
        machine="worker-a",
        worker_session_id="WORKER12",
    )

    with pytest.raises(ValueError, match="PTY shell mode"):
        await shell_ops.bash_execute(control.session_id, "python -i", pty=True)


@pytest.mark.asyncio
async def test_remote_session_dispatch_surfaces_worker_error(monkeypatch):
    store = get_tool_session_store()
    store.clear()
    control = store.create_session(
        target="remote",
        workdir="/remote/project",
        machine="worker-a",
        worker_session_id="WORKER12",
    )

    async def fake_call(machine, tool, args, timeout_s=None):
        return {
            "ok": True,
            "data": {
                "status": "error",
                "error_type": "FileNotFoundError",
                "message": "missing.txt",
            },
        }

    monkeypatch.setattr(
        "workgate.ops.utils.remote_session.call_remote_worker_tool",
        fake_call,
    )

    with pytest.raises(RuntimeError, match="FileNotFoundError: missing.txt"):
        await read_ops.read_execute("missing.txt", control.session_id)


@pytest.mark.asyncio
async def test_remote_session_change_cwd_preserves_worker_orientation(
    monkeypatch, tmp_path
):
    from workgate.ops.utils import remote_session as remote_session_ops

    remote_root = tmp_path / "worker-root"
    first = remote_root / "first"
    second = remote_root / "second"
    first.mkdir(parents=True)
    second.mkdir()
    (second / "AGENTS.md").write_text("worker instructions\n", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(remote_root))
    monkeypatch.setenv("WORKGATE_REMOTE_WORKER_RUNTIME", "1")
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()

    worker = session_ops._start_local_session(
        workdir=str(first), machine=None, label="remote"
    )
    control = store.create_session(
        target="remote",
        workdir=str(first),
        machine="worker-a",
        worker_session_id=worker.session_id,
        label="remote",
    )
    calls = []

    async def fake_call(machine, tool, args, timeout_s=None):
        calls.append((machine, tool, args, timeout_s))
        changed = await session_ops.session_change_cwd_execute(
            worker.session_id, str(second)
        )
        return {"ok": True, "data": changed.model_dump(mode="json")}

    monkeypatch.setattr(
        remote_session_ops, "call_remote_worker_tool", fake_call
    )

    result = await session_ops.session_change_cwd_execute(
        control.session_id, "second"
    )

    assert calls == [
        (
            "worker-a",
            "session_change_cwd",
            {"workdir": "second", "session_id": worker.session_id},
            None,
        )
    ]
    assert result.session_id == control.session_id
    assert result.target == "remote"
    assert result.machine == "worker-a"
    assert result.workdir == str(second)
    assert result.workspace_root == str(remote_root)
    assert result.instruction_files == ["second/AGENTS.md"]
    assert result.environment.workspace.target == "remote"
    assert result.environment.workspace.machine == "worker-a"
    assert result.environment.workspace.worker_runtime.active is True
    assert store.require_session(control.session_id).workdir == str(second)
