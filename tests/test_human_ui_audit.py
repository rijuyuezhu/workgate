import base64
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import workgate.tools.registry.remote as remote_registry_module
import workgate.ui.http.audit as ui_audit_module
import workgate.ui.http.common as ui_common_module
import workgate.ui.http.session_snapshot as ui_session_snapshot_module
from workgate.audit import (
    audit,
    audit_tool_call_end,
    audit_tool_call_start,
)
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.core.scopes import (
    SCOPE_AUDIT_FULL,
    SCOPE_AUDIT_READ,
    SCOPE_REMOTE_USE,
    SCOPE_SHELL_EXECUTE,
    SCOPE_SHELL_WRITE,
)
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.ops.todo import write_todos_execute
from workgate.remote.tool_specs import (
    REMOTE_WORKER_ORIGIN_ARG,
    REMOTE_WORKER_ORIGIN_HUMAN_UI,
)
from workgate.remote_worker.dispatch import execute_worker_tool
from workgate.schemas.result_models.remote import (
    RemoteListMachinesOutput,
    RemoteMachineInfo,
)
from workgate.tool_session.store import get_tool_session_store

BASE_URL = "https://workgate.example"


@pytest.fixture(autouse=True)
def _reset_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _configure(
    monkeypatch,
    workspace: Path,
    *,
    auth_mode: str = "none",
    remote_enabled: bool = False,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(workspace / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv(
        "WORKGATE_REMOTE_ENABLED", "true" if remote_enabled else "false"
    )
    clear_settings_cache()


def _client(
    monkeypatch,
    tmp_path: Path,
    *,
    auth_mode: str = "none",
    remote_enabled: bool = False,
) -> TestClient:
    _configure(
        monkeypatch,
        tmp_path / "workspace",
        auth_mode=auth_mode,
        remote_enabled=remote_enabled,
    )
    return TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.14", 50005),
    )


def _token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-audit-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def _headers(scope: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(scope)}"}


def test_local_audit_lists_filters_and_returns_details(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    audit(
        "tool_call_start",
        call_id="local-read",
        tool="read",
        transport="http",
        session_id="session-local",
        input={"path": "alpha.txt"},
    )
    audit(
        "tool_call_end",
        call_id="local-read",
        tool="read",
        transport="http",
        ok=True,
        duration_ms=4,
        output={"content": "alpha"},
    )
    audit("job_started", job_id="job-1", command="compile beta")

    response = client.get(
        "/api/ui/audit",
        params={
            "machine": "local",
            "operation": "files",
            "session": "session-local",
            "search": "READ",
            "sort": "asc",
            "limit": 100,
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["machine"] == "local"
    assert payload["remote"] is False
    assert payload["count"] == 1
    assert payload["total_matched"] == 1
    summary = payload["entries"][0]
    assert summary["id"] == "call:local-read"
    assert summary["node"] == "local"
    assert "input" not in summary
    assert "output" not in summary
    assert "error" not in summary

    detail = client.get(
        "/api/ui/audit/detail",
        params={"machine": "local", "id": "call:local-read"},
    )
    assert detail.status_code == 200
    entry = detail.json()["data"]["entry"]
    assert entry["input"] == {"path": "alpha.txt"}
    assert entry["output"] == {"content": "alpha"}
    assert entry["status"] == "success"


def test_audit_detail_enforces_operation_sensitive_scopes(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path, auth_mode="oauth")
    audit(
        "tool_call_start",
        call_id="local-write",
        tool="write_file",
        transport="http",
        input={"path": "notes.txt", "content": "hello"},
    )
    audit(
        "tool_call_end",
        call_id="local-write",
        tool="write_file",
        transport="http",
        ok=True,
        duration_ms=3,
        output={"path": "notes.txt"},
    )

    read_only = _headers(SCOPE_AUDIT_READ)
    read_write = _headers(f"{SCOPE_AUDIT_READ} {SCOPE_SHELL_WRITE}")

    listing = client.get("/api/ui/audit", headers=read_only)
    denied = client.get(
        "/api/ui/audit/detail",
        params={"id": "call:local-write"},
        headers=read_only,
    )
    allowed = client.get(
        "/api/ui/audit/detail",
        params={"id": "call:local-write"},
        headers=read_write,
    )

    assert listing.status_code == 200
    assert denied.status_code == 403
    assert SCOPE_SHELL_WRITE in denied.text
    assert allowed.status_code == 200


def test_audit_detail_full_payloads_require_audit_full(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, auth_mode="oauth")
    audit(
        "job_started",
        job_id="large-job",
        command="x" * 20_000,
    )
    entry_id = client.get(
        "/api/ui/audit",
        headers=_headers(SCOPE_AUDIT_READ),
    ).json()["data"]["entries"][0]["id"]

    denied = client.get(
        "/api/ui/audit/detail",
        params={"id": entry_id, "include_full_payloads": "true"},
        headers=_headers(f"{SCOPE_AUDIT_READ} {SCOPE_SHELL_EXECUTE}"),
    )
    allowed = client.get(
        "/api/ui/audit/detail",
        params={"id": entry_id, "include_full_payloads": "true"},
        headers=_headers(
            f"{SCOPE_AUDIT_READ} {SCOPE_AUDIT_FULL} {SCOPE_SHELL_EXECUTE}"
        ),
    )

    assert denied.status_code == 403
    assert SCOPE_AUDIT_FULL in denied.text
    assert allowed.status_code == 200
    command = allowed.json()["data"]["entry"]["command"]
    assert command == "x" * 20_000


class _FakeManager:
    def __init__(self, status: str = "online") -> None:
        self.status = status

    def list_machines(self) -> RemoteListMachinesOutput:
        return RemoteListMachinesOutput(
            machines=[
                RemoteMachineInfo(
                    name="edge",
                    status=self.status,
                    workdir="/srv/workspace",
                    last_seen=1.0,
                    queue_depth=0,
                    capabilities=["audit"],
                    info={},
                )
            ],
            counts={self.status: 1, "total": 1},
        )


class _FakeRemoteAudit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.malformed = False
        self.worker_session_id = ""

    async def call(
        self,
        machine: str,
        tool: str,
        args: dict[str, Any],
        timeout_s: int,
    ) -> dict[str, Any]:
        assert machine == "edge"
        assert 1 <= timeout_s <= 60
        assert "session_id" not in args
        self.calls.append((tool, dict(args), timeout_s))
        if self.malformed:
            return {"ok": True, "data": {"entries": "bad"}}
        entry = {
            "id": "call:remote-shell",
            "ts": 2.0,
            "event": "tool_call",
            "tool": "bash",
            "operation": "shell",
            "status": "success",
            "paired": True,
            "input": {"command": "printf safe"},
            "output": {"stdout": "safe"},
        }
        if self.worker_session_id:
            entry["session"] = self.worker_session_id
        if tool == "query_audit":
            list_entry = (
                {
                    name: value
                    for name, value in entry.items()
                    if name not in {"input", "output"}
                }
                if args.get("summary_only")
                else entry
            )
            data = {
                "entries": [list_entry],
                "count": 1,
                "total_matched": 1,
            }
            if args.get("snapshot"):
                data["entry"] = entry
            return {
                "ok": True,
                "data": data,
            }
        assert tool == "get_audit_entry"
        expected = {
            "id": "call:remote-shell",
            "include_full_payloads": False,
        }
        if self.worker_session_id and args.get("log_session_id"):
            expected["log_session_id"] = self.worker_session_id
        assert args == expected
        return {"ok": True, "data": entry}


def _remote_client(
    monkeypatch,
    tmp_path: Path,
    fake: _FakeRemoteAudit,
    *,
    auth_mode: str = "none",
    status: str = "online",
) -> TestClient:
    client = _client(
        monkeypatch,
        tmp_path,
        auth_mode=auth_mode,
        remote_enabled=True,
    )
    monkeypatch.setattr(
        ui_common_module,
        "remote_manager",
        lambda: _FakeManager(status=status),
    )
    monkeypatch.setattr(ui_audit_module, "call_remote_worker_tool", fake.call)
    return client


def test_remote_audit_uses_process_scoped_native_worker_rpc(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteAudit()
    client = _remote_client(monkeypatch, tmp_path, fake)

    listing = client.get(
        "/api/ui/audit",
        params={"machine": "edge", "operation": "shell", "limit": 100},
    )
    detail = client.get(
        "/api/ui/audit/detail",
        params={"machine": "edge", "id": "call:remote-shell"},
    )

    assert listing.status_code == 200
    remote_summary = listing.json()["data"]["entries"][0]
    assert remote_summary["node"] == "edge"
    assert "input" not in remote_summary
    assert "output" not in remote_summary
    assert listing.json()["data"]["remote"] is True
    assert detail.status_code == 200
    assert detail.json()["data"]["entry"]["node"] == "edge"
    assert [call[0] for call in fake.calls] == [
        "query_audit",
        "get_audit_entry",
    ]
    assert fake.calls[0][1]["operation"] == "shell"
    assert fake.calls[0][1]["summary_only"] is True


def test_remote_audit_snapshot_returns_selected_preview_in_one_rpc(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteAudit()
    client = _remote_client(monkeypatch, tmp_path, fake)

    response = client.get(
        "/api/ui/audit",
        params={
            "machine": "edge",
            "operation": "shell",
            "include_selected": "true",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["entries"][0]["id"] == "call:remote-shell"
    assert "input" not in data["entries"][0]
    assert data["entry"]["input"] == {"command": "printf safe"}
    assert data["entry"]["output"] == {"stdout": "safe"}
    assert [call[0] for call in fake.calls] == ["query_audit"]
    assert fake.calls[0][1]["snapshot"] is True


def test_local_session_snapshot_returns_todos_and_selected_audit_preview(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    session = get_tool_session_store().create_session(
        workdir=workspace,
        label="local snapshot",
    )
    write_todos_execute(
        [
            {
                "id": "todo-1",
                "content": "inspect snapshot",
                "status": "in_progress",
                "priority": "high",
            }
        ],
        session.session_id,
        expected_revision=0,
        touch_session=False,
    )
    call_id = "local-session-snapshot"
    session_ids = audit_tool_call_start(
        call_id=call_id,
        transport="http",
        tool="read",
        input={"session_id": session.session_id, "path": "notes.txt"},
    )
    audit_tool_call_end(
        call_id=call_id,
        transport="http",
        tool="read",
        ok=True,
        duration_ms=2,
        output={"content": "snapshot"},
        session_ids=session_ids,
    )

    response = client.get(
        "/api/ui/sessions/snapshot",
        params={
            "machine": "local",
            "session_id": session.session_id,
            "operation": "files",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["todos"][0]["content"] == "inspect snapshot"
    assert data["audit"]["entries"][0]["id"] == f"call:{call_id}"
    assert "input" not in data["audit"]["entries"][0]
    assert data["audit"]["entry"]["input"]["path"] == "notes.txt"
    assert data["audit"]["entry"]["output"] == {"content": "snapshot"}


def test_session_snapshot_offloads_selected_detail_projection(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    session = get_tool_session_store().create_session(
        workdir=workspace,
        label="threaded snapshot",
    )
    call_id = "threaded-session-snapshot"
    session_ids = audit_tool_call_start(
        call_id=call_id,
        transport="http",
        tool="read",
        input={"session_id": session.session_id, "path": "threaded.txt"},
    )
    audit_tool_call_end(
        call_id=call_id,
        transport="http",
        tool="read",
        ok=True,
        duration_ms=1,
        output={"content": "threaded"},
        session_ids=session_ids,
    )

    original_to_thread = ui_session_snapshot_module.asyncio.to_thread
    thread_calls: list[tuple[str, int, int]] = []

    async def tracked_to_thread(function, /, *args, **kwargs):
        caller_thread = threading.get_ident()

        def invoke():
            worker_thread = threading.get_ident()
            thread_calls.append(
                (
                    getattr(function, "__name__", repr(function)),
                    caller_thread,
                    worker_thread,
                )
            )
            return function(*args, **kwargs)

        return await original_to_thread(invoke)

    monkeypatch.setattr(
        ui_session_snapshot_module.asyncio,
        "to_thread",
        tracked_to_thread,
    )

    response = client.get(
        "/api/ui/sessions/snapshot",
        params={
            "machine": "local",
            "session_id": session.session_id,
            "operation": "files",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    detail_call = next(
        call for call in thread_calls if call[0] == "_audit_view_image_detail"
    )
    assert detail_call[1] != detail_call[2]


def test_remote_session_snapshot_uses_one_session_worker_rpc(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteAudit()
    client = _remote_client(monkeypatch, tmp_path, fake)
    session = get_tool_session_store().create_session(
        target="remote",
        machine="edge",
        workdir="/srv/project",
        worker_session_id="worker01",
        label="remote snapshot",
    )
    calls: list[tuple[str, dict[str, Any], int | None, str]] = []

    async def snapshot_call(
        called_session,
        tool: str,
        args: dict[str, Any],
        timeout_s: int | None = None,
        *,
        audit_origin: str,
    ) -> dict[str, Any]:
        assert called_session.session_id == session.session_id
        calls.append((tool, dict(args), timeout_s, audit_origin))
        entry = {
            "id": "call:remote-snapshot",
            "ts": 3.0,
            "event": "tool_call",
            "tool": "bash",
            "operation": "shell",
            "session": "worker01",
            "input": {"command": "printf snapshot"},
            "output": {"stdout": "snapshot"},
        }
        return {
            "todos": {
                "revision": 4,
                "updated_at": 2.0,
                "todos": [
                    {
                        "id": "remote-todo",
                        "content": "remote state",
                        "status": "pending",
                        "priority": "medium",
                    }
                ],
            },
            "audit": {
                "entries": [entry],
                "count": 1,
                "total_matched": 1,
                "entry": entry,
            },
        }

    monkeypatch.setattr(
        ui_session_snapshot_module,
        "call_remote_session_tool",
        snapshot_call,
    )

    response = client.get(
        "/api/ui/sessions/snapshot",
        params={
            "machine": "edge",
            "session_id": session.session_id,
            "operation": "shell",
            "limit": 25,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["todos"][0]["content"] == "remote state"
    assert data["audit"]["entries"][0]["session"] == session.session_id
    assert data["audit"]["entry"]["input"] == {"command": "printf snapshot"}
    assert len(calls) == 1
    tool, args, timeout_s, origin = calls[0]
    assert tool == "ui_session_snapshot"
    assert args["operation"] == "shell"
    assert args["limit"] == 25
    assert timeout_s is not None
    assert origin == REMOTE_WORKER_ORIGIN_HUMAN_UI


def test_remote_audit_maps_public_session_filters_to_worker_ids(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteAudit()
    fake.worker_session_id = "worker01"
    client = _remote_client(monkeypatch, tmp_path, fake)
    session = get_tool_session_store().create_session(
        target="remote",
        machine="edge",
        workdir="/srv/project",
        worker_session_id=fake.worker_session_id,
        label="remote audit",
    )

    listing = client.get(
        "/api/ui/audit",
        params={"machine": "edge", "session": session.session_id},
    )

    assert listing.status_code == 200
    assert fake.calls[0][1]["session"] == fake.worker_session_id
    assert listing.json()["data"]["entries"][0]["session"] == session.session_id


@pytest.mark.asyncio
async def test_remote_audit_builds_session_projection_once_per_query(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace, remote_enabled=True)
    store = get_tool_session_store()
    session = store.create_session(
        target="remote",
        machine="edge",
        workdir="/srv/project",
        worker_session_id="worker01",
    )
    list_calls = 0
    original_list_sessions = store.list_sessions

    def counted_list_sessions():
        nonlocal list_calls
        list_calls += 1
        return original_list_sessions()

    async def remote_query(
        machine: str, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert machine == "edge"
        assert tool == "query_audit"
        assert args == {"summary_only": True}
        entries = [
            {
                "id": f"call:remote-{index}",
                "ts": float(index),
                "event": "tool_call",
                "operation": "shell",
                "session": "worker01",
            }
            for index in range(3)
        ]
        return {"entries": entries, "count": 3, "total_matched": 3}

    monkeypatch.setattr(store, "list_sessions", counted_list_sessions)
    monkeypatch.setattr(ui_audit_module, "_remote_audit_call", remote_query)

    result = await ui_audit_module._query("edge", {})

    assert list_calls == 1
    assert {entry["session"] for entry in result["entries"]} == {
        session.session_id
    }


def test_remote_session_audit_uses_worker_local_log_and_public_projection(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteAudit()
    fake.worker_session_id = "worker01"
    client = _remote_client(monkeypatch, tmp_path, fake)
    session = get_tool_session_store().create_session(
        target="remote",
        machine="edge",
        workdir="/srv/project",
        worker_session_id=fake.worker_session_id,
        label="remote local audit",
    )

    listing = client.get(
        "/api/ui/audit",
        params={
            "machine": "edge",
            "scope": "session",
            "session": session.session_id,
        },
    )
    detail = client.get(
        "/api/ui/audit/detail",
        params={
            "machine": "edge",
            "scope": "session",
            "session": session.session_id,
            "id": "call:remote-shell",
        },
    )

    assert listing.status_code == 200
    assert listing.json()["data"]["scope"] == "session"
    assert listing.json()["data"]["entries"][0]["session"] == session.session_id
    assert "session" not in fake.calls[0][1]
    assert fake.calls[0][1]["log_session_id"] == fake.worker_session_id
    assert detail.status_code == 200
    assert detail.json()["data"]["scope"] == "session"
    assert fake.calls[1][1]["log_session_id"] == fake.worker_session_id


def test_session_audit_scope_keeps_multi_session_calls_in_each_owner_log(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    store = get_tool_session_store()
    first = store.create_session(workdir=workspace, label="source")
    second = store.create_session(workdir=workspace, label="destination")
    call_id = "multi-session-copy"
    session_ids = audit_tool_call_start(
        call_id=call_id,
        transport="http",
        tool="session_copy",
        input={
            "src_session_id": first.session_id,
            "dst_session_id": second.session_id,
            "src_path": "source.txt",
            "dst_path": "destination.txt",
        },
    )
    audit_tool_call_end(
        call_id=call_id,
        transport="http",
        tool="session_copy",
        ok=True,
        duration_ms=1,
        output={"status": "copied"},
        session_ids=session_ids,
    )

    destination = client.get(
        "/api/ui/audit",
        params={
            "machine": "local",
            "scope": "session",
            "session": second.session_id,
        },
    )

    assert destination.status_code == 200
    assert destination.json()["data"]["count"] == 1
    entry = destination.json()["data"]["entries"][0]
    assert entry["id"] == f"call:{call_id}"
    assert entry["session"] == second.session_id


def test_remote_audit_scopes_offline_and_malformed_returns(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteAudit()
    client = _remote_client(
        monkeypatch,
        tmp_path,
        fake,
        auth_mode="oauth",
    )
    read_only = _headers(SCOPE_AUDIT_READ)
    remote_read = _headers(f"{SCOPE_AUDIT_READ} {SCOPE_REMOTE_USE}")
    remote_execute = _headers(
        f"{SCOPE_AUDIT_READ} {SCOPE_SHELL_EXECUTE} {SCOPE_REMOTE_USE}"
    )

    missing_remote = client.get(
        "/api/ui/audit",
        params={"machine": "edge"},
        headers=read_only,
    )
    readable = client.get(
        "/api/ui/audit",
        params={"machine": "edge"},
        headers=remote_read,
    )
    missing_execute = client.get(
        "/api/ui/audit/detail",
        params={"machine": "edge", "id": "call:remote-shell"},
        headers=remote_read,
    )
    executable = client.get(
        "/api/ui/audit/detail",
        params={"machine": "edge", "id": "call:remote-shell"},
        headers=remote_execute,
    )

    assert missing_remote.status_code == 403
    assert SCOPE_REMOTE_USE in missing_remote.text
    assert readable.status_code == 200
    assert missing_execute.status_code == 403
    assert SCOPE_SHELL_EXECUTE in missing_execute.text
    assert executable.status_code == 200

    fake.malformed = True
    malformed = client.get(
        "/api/ui/audit",
        params={"machine": "edge"},
        headers=remote_read,
    )
    assert malformed.status_code == 502
    assert "malformed audit entries" in malformed.text

    offline_fake = _FakeRemoteAudit()
    offline_client = _remote_client(
        monkeypatch,
        tmp_path / "offline",
        offline_fake,
        status="offline",
    )
    offline = offline_client.get("/api/ui/audit", params={"machine": "edge"})
    assert offline.status_code == 503
    assert "offline" in offline.text
    assert offline_fake.calls == []


@pytest.mark.asyncio
async def test_remote_worker_dispatch_exposes_process_scoped_audit(
    monkeypatch, tmp_path
):
    _configure(monkeypatch, tmp_path / "worker")
    audit(
        "tool_call_start",
        call_id="worker-read",
        tool="read",
        transport="worker",
        input={"path": "remote.txt"},
    )
    audit(
        "tool_call_end",
        call_id="worker-read",
        tool="read",
        transport="worker",
        ok=True,
        duration_ms=1,
        output={"content": "remote"},
    )

    listing = await execute_worker_tool(
        "query_audit", {"operation": "files", "limit": 10}
    )
    detail = await execute_worker_tool(
        "get_audit_entry", {"id": "call:worker-read"}
    )

    assert listing["count"] == 1
    assert listing["entries"][0]["id"] == "call:worker-read"
    assert detail["output"] == {"content": "remote"}


@pytest.mark.asyncio
async def test_remote_worker_dispatch_returns_session_snapshot_in_one_job(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "worker"
    _configure(monkeypatch, workspace)
    session = get_tool_session_store().create_session(
        workdir=workspace,
        label="worker snapshot",
    )
    write_todos_execute(
        [
            {
                "id": "worker-todo",
                "content": "return combined state",
                "status": "pending",
                "priority": "high",
            }
        ],
        session.session_id,
        expected_revision=0,
        touch_session=False,
    )
    call_id = "worker-snapshot-read"
    session_ids = audit_tool_call_start(
        call_id=call_id,
        transport="worker",
        tool="read",
        input={"session_id": session.session_id, "path": "remote.txt"},
    )
    audit_tool_call_end(
        call_id=call_id,
        transport="worker",
        tool="read",
        ok=True,
        duration_ms=1,
        output={"content": "remote"},
        session_ids=session_ids,
    )

    snapshot = await execute_worker_tool(
        "ui_session_snapshot",
        {
            "session_id": session.session_id,
            "operation": "files",
            "limit": 10,
            REMOTE_WORKER_ORIGIN_ARG: REMOTE_WORKER_ORIGIN_HUMAN_UI,
        },
    )

    assert snapshot["todos"]["todos"][0]["content"] == ("return combined state")
    assert snapshot["audit"]["entries"][0]["id"] == f"call:{call_id}"
    assert "input" not in snapshot["audit"]["entries"][0]
    assert snapshot["audit"]["entry"]["input"]["path"] == "remote.txt"


@pytest.mark.asyncio
async def test_remote_worker_dispatch_records_session_owned_tool_lifecycle(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "worker"
    _configure(monkeypatch, workspace)
    session = get_tool_session_store().create_session(
        workdir=workspace,
        label="worker audit owner",
    )
    (workspace / "remote.txt").write_text("remote content", encoding="utf-8")

    result = await execute_worker_tool(
        "read",
        {"session_id": session.session_id, "path": "remote.txt"},
    )
    listing = await execute_worker_tool(
        "query_audit",
        {
            "log_session_id": session.session_id,
            "operation": "files",
            "limit": 10,
        },
    )

    assert "remote content" in result.content
    assert listing["count"] == 1
    entry = listing["entries"][0]
    assert entry["tool"] == "read"
    assert entry["session"] == session.session_id
    assert entry["status"] == "success"


@pytest.mark.asyncio
async def test_remote_worker_dispatch_excludes_human_ui_calls_from_model_audit(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "worker"
    _configure(monkeypatch, workspace)
    session = get_tool_session_store().create_session(
        workdir=workspace,
        label="human ui worker session",
    )
    (workspace / "remote.txt").write_text("remote content", encoding="utf-8")

    result = await execute_worker_tool(
        "read",
        {
            "session_id": session.session_id,
            "path": "remote.txt",
            REMOTE_WORKER_ORIGIN_ARG: REMOTE_WORKER_ORIGIN_HUMAN_UI,
        },
    )
    listing = await execute_worker_tool(
        "query_audit",
        {
            "log_session_id": session.session_id,
            "operation": "files",
            "limit": 10,
        },
    )

    assert "remote content" in result.content
    assert listing["count"] == 0


@pytest.mark.asyncio
async def test_remote_registry_audit_handlers_support_global_and_session_logs(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace)
    store = get_tool_session_store()
    session = store.create_session(workdir=workspace, label="registry audit")
    call_id = "registry-session-read"
    session_ids = audit_tool_call_start(
        call_id=call_id,
        transport="worker",
        tool="read",
        input={"session_id": session.session_id, "path": "file.txt"},
    )
    audit_tool_call_end(
        call_id=call_id,
        transport="worker",
        tool="read",
        ok=True,
        duration_ms=1,
        output={"path": "file.txt"},
        session_ids=session_ids,
    )

    global_listing = await remote_registry_module._query_audit_handler(
        {"operation": "files", "limit": 10}
    )
    session_listing = await remote_registry_module._query_audit_handler(
        {
            "log_session_id": session.session_id,
            "operation": "files",
            "limit": 10,
        }
    )
    global_detail = await remote_registry_module._get_audit_entry_handler(
        {"id": f"call:{call_id}"}
    )
    session_detail = await remote_registry_module._get_audit_entry_handler(
        {
            "log_session_id": session.session_id,
            "id": f"call:{call_id}",
        }
    )

    assert global_listing["count"] == 1
    assert session_listing["count"] == 1
    assert global_detail["id"] == f"call:{call_id}"
    assert session_detail["id"] == f"call:{call_id}"


@pytest.mark.asyncio
async def test_remote_registry_internal_adapters_forward_normalized_arguments(
    monkeypatch,
):
    calls: list[tuple[str, tuple[Any, ...]]] = []

    monkeypatch.setattr(
        remote_registry_module,
        "dashboard_snapshot",
        lambda: {"status": "ok"},
    )

    async def record(name: str, *args: Any) -> dict[str, Any]:
        calls.append((name, args))
        return {"name": name, "args": list(args)}

    monkeypatch.setattr(
        remote_registry_module,
        "start_persistent_shell_execute",
        lambda cwd, name, command: record("start", cwd, name, command),
    )
    monkeypatch.setattr(
        remote_registry_module,
        "open_terminal_bridge_execute",
        lambda shell_id, cols, rows: record("open", shell_id, cols, rows),
    )
    monkeypatch.setattr(
        remote_registry_module,
        "read_terminal_bridge_execute",
        lambda bridge_id, max_bytes, wait_ms: record(
            "read", bridge_id, max_bytes, wait_ms
        ),
    )
    monkeypatch.setattr(
        remote_registry_module,
        "write_terminal_bridge_execute",
        lambda bridge_id, data_b64: record("write", bridge_id, data_b64),
    )
    monkeypatch.setattr(
        remote_registry_module,
        "resize_terminal_bridge_execute",
        lambda bridge_id, cols, rows: record("resize", bridge_id, cols, rows),
    )
    monkeypatch.setattr(
        remote_registry_module,
        "close_terminal_bridge_execute",
        lambda bridge_id: record("close", bridge_id),
    )
    monkeypatch.setattr(
        remote_registry_module,
        "remote_admin_execute",
        lambda action, args: record("admin", action, args),
    )
    monkeypatch.setattr(
        remote_registry_module,
        "remote_worker_tool_execute",
        lambda args, tool_name, timeout_s: record(
            "worker", args, tool_name, timeout_s
        ),
    )

    assert await remote_registry_module._dashboard_snapshot_handler({}) == {
        "status": "ok"
    }
    await remote_registry_module._start_persistent_shell_handler(
        {"cwd": "/tmp", "name": "demo", "command": "printf ok"}
    )
    await remote_registry_module._open_terminal_bridge_handler(
        {"shell_id": "shell1", "cols": 90, "rows": 30}
    )
    await remote_registry_module._read_terminal_bridge_handler(
        {"bridge_id": "bridge1", "max_bytes": 100, "wait_ms": 5}
    )
    await remote_registry_module._write_terminal_bridge_handler(
        {"bridge_id": "bridge1", "data_b64": "YWJj"}
    )
    await remote_registry_module._resize_terminal_bridge_handler(
        {"bridge_id": "bridge1", "cols": 100, "rows": 40}
    )
    await remote_registry_module._close_terminal_bridge_handler(
        {"bridge_id": "bridge1"}
    )
    await remote_registry_module.remote_admin.func("list", {})
    handler = remote_registry_module._make_remote_worker_handler(
        "read_todos", timeout_arg="timeout_s", default_timeout=7
    )
    await handler({"timeout_s": 9, "session_id": "ABCDEFGH"})
    no_timeout_handler = remote_registry_module._make_remote_worker_handler(
        "dashboard_snapshot"
    )
    await no_timeout_handler({})

    assert calls == [
        ("start", ("/tmp", "demo", "printf ok")),
        ("open", ("shell1", 90, 30)),
        ("read", ("bridge1", 100, 5)),
        ("write", ("bridge1", "YWJj")),
        ("resize", ("bridge1", 100, 40)),
        ("close", ("bridge1",)),
        ("admin", ("list", {})),
        (
            "worker",
            (
                {"timeout_s": 9, "session_id": "ABCDEFGH"},
                "read_todos",
                9,
            ),
        ),
        ("worker", ({}, "dashboard_snapshot", None)),
    ]


def test_audit_api_validates_bounds_and_unknown_details(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    assert client.get("/api/ui/audit", params={"limit": 0}).status_code == 400
    assert (
        client.get("/api/ui/audit", params={"sort": "sideways"}).status_code
        == 400
    )
    assert (
        client.get(
            "/api/ui/audit", params={"start_ts": 2, "end_ts": 1}
        ).status_code
        == 400
    )
    missing = client.get("/api/ui/audit/detail", params={"id": "missing"})
    assert missing.status_code == 404


def test_audit_detail_sanitizes_and_previews_view_image(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
    )
    audit(
        "tool_call_start",
        call_id="local-image",
        tool="view_image",
        transport="http",
        input={"path": "pixel.png"},
    )
    audit(
        "tool_call_end",
        call_id="local-image",
        tool="view_image",
        transport="http",
        ok=True,
        output={
            "content": [
                {
                    "type": "image",
                    "data": base64.b64encode(png).decode("ascii"),
                    "mimeType": "image/png",
                }
            ],
            "structuredContent": {"path": "pixel.png"},
        },
    )

    response = client.get(
        "/api/ui/audit/detail",
        params={
            "id": "call:local-image",
            "columns": 10,
            "rows": 5,
            "cell_aspect": 2,
        },
    )

    assert response.status_code == 200
    entry = response.json()["data"]["entry"]
    assert "data" not in entry["output"]["content"][0]
    assert entry["output"]["content"][0]["bytes"] == len(png)
    preview = entry["image_preview"]
    assert preview["kind"] == "image"
    assert preview["path"] == "pixel.png"
    assert preview["bytes"] == len(png)
    assert preview["mime_type"] == "image/png"
    assert preview["data_base64"] == base64.b64encode(png).decode("ascii")
    assert len(base64.b64decode(preview["rgba"])) == (
        preview["width"] * preview["height"] * 4
    )


def test_audit_detail_rejects_invalid_inline_image(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    audit(
        "tool_call_start",
        call_id="invalid-image",
        tool="view_image",
        transport="http",
    )
    audit(
        "tool_call_end",
        call_id="invalid-image",
        tool="view_image",
        transport="http",
        ok=False,
        output={"content": [{"type": "image", "data": "not-base64"}]},
    )

    response = client.get(
        "/api/ui/audit/detail", params={"id": "call:invalid-image"}
    )

    assert response.status_code == 200
    entry = response.json()["data"]["entry"]
    assert "data" not in entry["output"]["content"][0]
    assert "image_preview" not in entry
    assert entry["image_preview_error"]


def test_audit_static_ui_avoids_html_injection_for_untrusted_details():
    static_root = (
        Path(__file__).parents[1] / "src" / "workgate" / "ui" / "static"
    )
    audit_script = (static_root / "audit.js").read_text(encoding="utf-8")
    audit_view = (static_root / "audit_view.js").read_text(encoding="utf-8")

    assert "innerHTML" not in audit_view
    assert "elements.auditDetailBody.innerHTML" not in audit_script
    assert "textContent" in audit_view
