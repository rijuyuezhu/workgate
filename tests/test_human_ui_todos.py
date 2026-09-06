import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import workgate.ops.todo as todo_module
import workgate.ops.utils.remote_session as remote_session_module
import workgate.ui.http.common as ui_common_module
import workgate.ui.http.todos as ui_todos_module
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.core.scopes import (
    SCOPE_REMOTE_USE,
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.ops.todo import (
    TodoConflictError,
    read_todos_execute,
    todo_counts_execute,
    write_todos_execute,
)
from workgate.remote.tool_specs import (
    REMOTE_WORKER_ORIGIN_ARG,
    REMOTE_WORKER_ORIGIN_HUMAN_UI,
)
from workgate.schemas.result_models.remote import (
    RemoteListMachinesOutput,
    RemoteMachineInfo,
)
from workgate.tool_session.store import (
    SESSION_ACTIVE_WINDOW_S,
    SESSION_TERMINATION_PROMPT,
    AgentSession,
    UnknownAgentSessionError,
    get_tool_session_store,
)

BASE_URL = "https://workgate.example"


@pytest.fixture(autouse=True)
def _reset_state():
    clear_settings_cache()
    get_tool_session_store().clear()
    yield
    clear_settings_cache()
    get_tool_session_store().clear()


def _configure(
    monkeypatch,
    workspace: Path,
    *,
    auth_mode: str = "none",
    remote_enabled: bool = False,
    max_todos: int | None = None,
    max_todo_bytes: int | None = None,
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
    if max_todos is not None:
        monkeypatch.setenv("WORKGATE_MAX_TODOS", str(max_todos))
    if max_todo_bytes is not None:
        monkeypatch.setenv("WORKGATE_MAX_TODO_BYTES", str(max_todo_bytes))
    clear_settings_cache()


def _client(monkeypatch, tmp_path, *, auth_mode: str = "none") -> TestClient:
    _configure(monkeypatch, tmp_path / "workspace", auth_mode=auth_mode)
    return TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.14", 50004),
    )


def _token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-todos-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def _item(identifier: str, content: str = "ship it") -> dict[str, str]:
    return {
        "id": identifier,
        "content": content,
        "status": "pending",
        "priority": "high",
    }


def _local_session(workspace: Path, *, label: str = "local") -> AgentSession:
    return get_tool_session_store().create_session(
        target="local", workdir=workspace, label=label
    )


def test_local_todos_require_explicit_session_and_use_session_directory(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace)
    session = _local_session(workspace)
    client = TestClient(build_http_app(), base_url=BASE_URL)
    params = {"machine": "local", "session_id": session.session_id}

    missing = client.get("/api/ui/todos", params={"machine": "local"})
    initial = client.get("/api/ui/todos", params=params)
    saved = client.put(
        "/api/ui/todos",
        json={
            **params,
            "expected_revision": 0,
            "todos": [_item("one")],
        },
    )
    stale = client.put(
        "/api/ui/todos",
        json={
            **params,
            "expected_revision": 0,
            "todos": [_item("stale", "must not win")],
        },
    )
    current = client.get("/api/ui/todos", params=params)

    assert missing.status_code == 400
    assert "session_id" in missing.text
    assert initial.status_code == 200
    assert initial.json()["data"]["session_id"] == session.session_id
    assert initial.json()["data"]["revision"] == 0
    assert initial.json()["data"]["todos"] == []
    assert saved.status_code == 200
    assert saved.json()["data"]["revision"] == 1
    assert stale.status_code == 409
    assert stale.json()["error"] == "TodoConflictError"
    assert current.json()["data"]["todos"][0]["id"] == "one"

    todo_path = (
        workspace / ".state" / "sessions" / session.session_id / "todos.json"
    )
    assert todo_path.is_file()
    assert (todo_path.parent / "session.json").is_file()
    if os.name != "nt":
        assert stat.S_IMODE(todo_path.stat().st_mode) == 0o600
    assert not list(todo_path.parent.glob("*.tmp"))
    assert not (workspace / ".state" / "todos").exists()
    assert not (workspace / ".state" / "todos.json").exists()


def test_todos_are_isolated_between_ui_selected_sessions(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace)
    first = _local_session(workspace, label="first")
    second = _local_session(workspace, label="second")
    client = TestClient(build_http_app(), base_url=BASE_URL)

    saved = client.put(
        "/api/ui/todos",
        json={
            "machine": "local",
            "session_id": first.session_id,
            "expected_revision": 0,
            "todos": [_item("first")],
        },
    )
    second_state = client.get(
        "/api/ui/todos",
        params={"machine": "local", "session_id": second.session_id},
    )

    assert saved.status_code == 200
    assert second_state.status_code == 200
    assert second_state.json()["data"]["todos"] == []


def test_todo_api_validates_shape_count_ids_and_encoded_lengths(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace, max_todos=2)
    session = _local_session(workspace)
    client = TestClient(build_http_app(), base_url=BASE_URL)
    base = {"machine": "local", "session_id": session.session_id}

    not_array = client.put(
        "/api/ui/todos",
        json={**base, "expected_revision": 0, "todos": {}},
    )
    too_many = client.put(
        "/api/ui/todos",
        json={
            **base,
            "expected_revision": 0,
            "todos": [_item("a"), _item("b"), _item("c")],
        },
    )
    duplicate = client.put(
        "/api/ui/todos",
        json={
            **base,
            "expected_revision": 0,
            "todos": [_item("same"), _item("same")],
        },
    )
    long_content = client.put(
        "/api/ui/todos",
        json={
            **base,
            "expected_revision": 0,
            "todos": [_item("a", "é" * 8_193)],
        },
    )
    bad_revision = client.put(
        "/api/ui/todos",
        json={**base, "expected_revision": True, "todos": []},
    )

    assert not_array.status_code == 400
    assert "JSON array" in not_array.text
    assert too_many.status_code == 400
    assert "max is 2" in too_many.text
    assert duplicate.status_code == 400
    assert "duplicate todo id" in duplicate.text
    assert long_content.status_code == 400
    assert "encoded bytes" in long_content.text
    assert bad_revision.status_code == 400
    assert "non-negative integer" in bad_revision.text


def test_todo_core_limits_and_counts_ignore_remote_or_deleted_sessions(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(
        monkeypatch,
        workspace,
        max_todos=1,
        max_todo_bytes=240,
    )
    store = get_tool_session_store()
    live = _local_session(workspace, label="live")
    remote = store.create_session(
        target="remote",
        machine="edge",
        workdir="/srv/project",
        worker_session_id="worker01",
        label="remote",
    )
    stale = _local_session(workspace, label="stale")

    with pytest.raises(ValueError, match="max is 1"):
        write_todos_execute([_item("one"), _item("two")], live.session_id, 0)
    with pytest.raises(ValueError, match="non-negative integer"):
        write_todos_execute([], live.session_id, True)
    with pytest.raises(ValueError, match="todo bytes"):
        write_todos_execute([_item("large", "x" * 500)], live.session_id, 0)

    monkeypatch.setenv("WORKGATE_MAX_TODOS", "10")
    monkeypatch.setenv("WORKGATE_MAX_TODO_BYTES", "1000000")
    clear_settings_cache()
    write_todos_execute(
        [
            _item("open"),
            {
                "id": "done",
                "content": "finished",
                "status": "completed",
                "priority": "low",
            },
        ],
        live.session_id,
        0,
    )
    get_state_store = todo_module.get_state_store
    get_state_store().layout.session_metadata_path(stale.session_id).unlink()
    monkeypatch.setattr(
        store,
        "list_sessions",
        lambda: [remote, stale, store.require_session(live.session_id)],
    )

    assert todo_counts_execute() == {"total": 2, "open": 1}
    with pytest.raises(UnknownAgentSessionError, match="unknown session_id"):
        todo_module._require_session_metadata("ABCDEFGH")


def test_todo_ui_validators_and_session_machine_ownership(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace, remote_enabled=True)
    local = _local_session(workspace)
    remote = get_tool_session_store().create_session(
        target="remote",
        machine="edge",
        workdir="/srv/project",
        worker_session_id="worker01",
    )

    with pytest.raises(ValueError, match="machine exceeds"):
        ui_todos_module._machine_arg("m" * 256)
    with pytest.raises(ValueError, match="alphanumeric"):
        ui_todos_module._session_id_arg("abcd-123")
    with pytest.raises(ValueError, match="must not be empty"):
        ui_todos_module._bounded_text(
            "",
            field="value",
            max_bytes=8,
            default="",
            allow_empty=False,
        )
    with pytest.raises(ValueError, match=r"todos\[0\] must be"):
        ui_todos_module._todo_items(["not-an-object"])
    with pytest.raises(ValueError, match="belongs to remote machine"):
        ui_todos_module._session_for_machine("local", remote.session_id)

    monkeypatch.setattr(
        ui_todos_module, "require_remote_machine", lambda _: None
    )
    with pytest.raises(ValueError, match="does not belong"):
        ui_todos_module._session_for_machine("edge", local.session_id)

    client = TestClient(build_http_app(), base_url=BASE_URL)
    response = client.put("/api/ui/todos", json=[])
    assert response.status_code == 400
    assert "JSON object" in response.text


@pytest.mark.asyncio
async def test_remote_todo_write_maps_validation_conflict_and_runtime_errors(
    monkeypatch,
):
    session = AgentSession(
        session_id="ABCDEFGH",
        target="remote",
        workdir="/srv/project",
        machine="edge",
        worker_session_id="WORKER01",
        created_at=1.0,
        updated_at=1.0,
    )

    async def malformed(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return {"revision": "bad", "todos": {}}

    monkeypatch.setattr(ui_todos_module, "call_remote_session_tool", malformed)
    with pytest.raises(RuntimeError, match="malformed todo state"):
        await ui_todos_module._write(session, [], 0)

    async def conflict(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError(
            "TodoConflictError: Todo list changed from revision 0 to 1"
        )

    monkeypatch.setattr(ui_todos_module, "call_remote_session_tool", conflict)
    with pytest.raises(TodoConflictError, match="changed from revision"):
        await ui_todos_module._write(session, [], 0)

    async def failure(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("worker failed")

    monkeypatch.setattr(ui_todos_module, "call_remote_session_tool", failure)
    with pytest.raises(RuntimeError, match="worker failed"):
        await ui_todos_module._write(session, [], 0)


def test_revision_guard_serializes_concurrent_replacements(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace)
    session = _local_session(workspace, label="concurrency")

    def replace(identifier: str):
        try:
            return write_todos_execute(
                [_item(identifier)], session.session_id, expected_revision=0
            )
        except TodoConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(replace, ["first", "second"]))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, TodoConflictError) for result in results) == 1
    current = read_todos_execute(session.session_id)
    assert current.revision == 1
    assert current.todos[0].id in {"first", "second"}


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
                    capabilities=["todos"],
                    info={},
                )
            ],
            counts={self.status: 1, "total": 1},
        )


class _FakeRemoteTodos:
    def __init__(self, worker_session_id: str) -> None:
        self.worker_session_id = worker_session_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.state: dict[str, Any] = {
            "revision": 0,
            "updated_at": None,
            "todos": [],
        }
        self.malformed = False

    async def call(
        self,
        machine: str,
        tool: str,
        args: dict[str, Any],
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        assert machine == "edge"
        assert timeout_s is None or timeout_s > 0
        assert args["session_id"] == self.worker_session_id
        self.calls.append((tool, dict(args)))
        if self.malformed:
            return {"ok": True, "data": {"revision": "bad", "todos": {}}}
        if tool == "read_todos":
            return {"ok": True, "data": dict(self.state)}
        assert tool == "write_todos"
        expected = args.get("expected_revision")
        revision = int(self.state["revision"])
        if expected != revision:
            return {
                "ok": True,
                "data": {
                    "status": "error",
                    "error_type": "TodoConflictError",
                    "message": (
                        f"Todo list changed from revision {expected} to {revision}; "
                        "reload before saving"
                    ),
                },
            }
        self.state = {
            "revision": revision + 1,
            "updated_at": 2.0,
            "todos": list(args.get("todos") or []),
        }
        return {"ok": True, "data": dict(self.state)}


def _remote_client(
    monkeypatch,
    tmp_path,
    fake: _FakeRemoteTodos,
    *,
    auth_mode: str = "none",
    status: str = "online",
) -> tuple[TestClient, AgentSession]:
    workspace = tmp_path / "workspace"
    _configure(
        monkeypatch,
        workspace,
        auth_mode=auth_mode,
        remote_enabled=True,
    )
    monkeypatch.setattr(
        ui_common_module, "remote_manager", lambda: _FakeManager(status=status)
    )
    monkeypatch.setattr(
        remote_session_module, "call_remote_worker_tool", fake.call
    )
    session = get_tool_session_store().create_session(
        target="remote",
        machine="edge",
        workdir="/srv/project",
        worker_session_id=fake.worker_session_id,
        label="remote agent",
    )
    client = TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.14", 50004),
    )
    return client, session


def test_session_end_waits_for_todo_write_and_removes_all_session_state(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace)
    store = get_tool_session_store()
    session = store.create_session(
        target="local", workdir=str(workspace), label="end-race"
    )
    entered = threading.Event()
    release = threading.Event()
    original_read = todo_module._read_todo_path

    def blocked_read(path):  # noqa: ANN001, ANN202
        entered.set()
        assert release.wait(timeout=5)
        return original_read(path)

    monkeypatch.setattr(todo_module, "_read_todo_path", blocked_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        write_future = executor.submit(
            write_todos_execute,
            [_item("racing")],
            session.session_id,
            0,
        )
        assert entered.wait(timeout=5)
        end_future = executor.submit(store.end_session, session.session_id)
        time.sleep(0.05)
        assert not end_future.done()
        release.set()
        assert write_future.result(timeout=5).revision == 1
        assert end_future.result(timeout=5).session_id == session.session_id

    assert not (workspace / ".state" / "sessions" / session.session_id).exists()
    with pytest.raises(ValueError, match="unknown session_id"):
        read_todos_execute(session.session_id)


def test_remote_todos_use_public_control_session_and_worker_binding(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteTodos("worker01")
    client, session = _remote_client(monkeypatch, tmp_path, fake)
    params = {"machine": "edge", "session_id": session.session_id}

    initial = client.get("/api/ui/todos", params=params)
    saved = client.put(
        "/api/ui/todos",
        json={**params, "expected_revision": 0, "todos": [_item("remote")]},
    )
    current = client.get("/api/ui/todos", params=params)

    assert initial.status_code == 200
    assert saved.status_code == 200
    assert current.json()["data"]["todos"][0]["id"] == "remote"
    assert current.json()["data"]["session_id"] == session.session_id
    assert {call[1]["session_id"] for call in fake.calls} == {"worker01"}
    assert {call[1][REMOTE_WORKER_ORIGIN_ARG] for call in fake.calls} == {
        REMOTE_WORKER_ORIGIN_HUMAN_UI
    }


def test_remote_todos_reject_wrong_machine_malformed_and_offline(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteTodos("worker01")
    client, session = _remote_client(monkeypatch, tmp_path, fake)
    params = {"machine": "edge", "session_id": session.session_id}

    wrong_machine = client.get(
        "/api/ui/todos",
        params={"machine": "other", "session_id": session.session_id},
    )
    fake.malformed = True
    malformed = client.get("/api/ui/todos", params=params)

    assert wrong_machine.status_code == 400
    assert malformed.status_code == 502
    assert "malformed todo state" in malformed.text

    offline_fake = _FakeRemoteTodos("worker02")
    offline_client, offline_session = _remote_client(
        monkeypatch, tmp_path / "offline", offline_fake, status="offline"
    )
    offline = offline_client.get(
        "/api/ui/todos",
        params={"machine": "edge", "session_id": offline_session.session_id},
    )
    assert offline.status_code == 503
    assert "offline" in offline.text
    assert offline_fake.calls == []


def test_sessions_api_lists_public_sessions_by_machine(monkeypatch, tmp_path):
    fake = _FakeRemoteTodos("worker01")
    client, remote = _remote_client(monkeypatch, tmp_path, fake)
    local = _local_session(tmp_path / "workspace", label="local agent")

    local_rows = client.get("/api/ui/sessions", params={"machine": "local"})
    remote_rows = client.get("/api/ui/sessions", params={"machine": "edge"})

    assert [
        row["session_id"] for row in local_rows.json()["data"]["sessions"]
    ] == [local.session_id]
    assert [
        row["session_id"] for row in remote_rows.json()["data"]["sessions"]
    ] == [remote.session_id]
    assert "worker_session_id" not in remote_rows.text


def test_sessions_api_terminates_offline_remote_session(monkeypatch, tmp_path):
    fake = _FakeRemoteTodos("worker01")
    client, remote = _remote_client(
        monkeypatch,
        tmp_path,
        fake,
        status="offline",
    )

    response = client.post(
        "/api/ui/sessions/terminate",
        json={"machine": "edge", "session_id": remote.session_id},
    )

    assert response.status_code == 200
    assert response.json()["data"]["session"]["termination_requested"]
    assert (
        get_tool_session_store()
        .require_session(remote.session_id)
        .termination_requested_at
    )
    assert fake.calls == []


def test_sessions_api_defaults_to_five_hour_activity_and_terminates_work(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    _configure(monkeypatch, workspace)
    store = get_tool_session_store()
    old = _local_session(workspace, label="old")
    active = _local_session(workspace, label="active")
    now = time.time()
    old_path = (
        workspace / ".state" / "sessions" / old.session_id / "session.json"
    )
    old_payload = json.loads(old_path.read_text(encoding="utf-8"))
    old_payload["updated_at"] = now - SESSION_ACTIVE_WINDOW_S - 1
    old_path.write_text(json.dumps(old_payload), encoding="utf-8")
    active_path = (
        workspace / ".state" / "sessions" / active.session_id / "session.json"
    )
    active_payload = json.loads(active_path.read_text(encoding="utf-8"))
    active_payload["updated_at"] = now
    active_path.write_text(json.dumps(active_payload), encoding="utf-8")
    client = TestClient(build_http_app(), base_url=BASE_URL)

    recent = client.get("/api/ui/sessions", params={"machine": "local"})
    all_sessions = client.get(
        "/api/ui/sessions",
        params={"machine": "local", "include_inactive": "true"},
    )
    old_todos = client.get(
        "/api/ui/todos",
        params={"machine": "local", "session_id": old.session_id},
    )
    recent_after_view = client.get(
        "/api/ui/sessions", params={"machine": "local"}
    )
    terminated = client.post(
        "/api/ui/sessions/terminate",
        json={"machine": "local", "session_id": active.session_id},
    )
    blocked = client.post(
        "/tools/read",
        json={"session_id": active.session_id, "path": "missing.txt"},
    )

    assert [row["session_id"] for row in recent.json()["data"]["sessions"]] == [
        active.session_id
    ]
    assert all_sessions.json()["data"]["active_window_hours"] == 5
    assert {
        row["session_id"] for row in all_sessions.json()["data"]["sessions"]
    } == {active.session_id, old.session_id}
    assert old_todos.status_code == 200
    assert [
        row["session_id"]
        for row in recent_after_view.json()["data"]["sessions"]
    ] == [active.session_id]
    assert terminated.status_code == 200
    assert terminated.json()["data"]["session"]["termination_requested"]
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "session_termination_requested"
    assert SESSION_TERMINATION_PROMPT in blocked.json()["message"]
    assert store.require_session(active.session_id).termination_requested_at


def test_todo_api_enforces_local_remote_and_write_scopes(monkeypatch, tmp_path):
    fake = _FakeRemoteTodos("worker01")
    client, remote = _remote_client(
        monkeypatch, tmp_path, fake, auth_mode="oauth"
    )
    local = _local_session(tmp_path / "workspace")
    local_params = {"machine": "local", "session_id": local.session_id}
    remote_params = {"machine": "edge", "session_id": remote.session_id}
    read_only = {"Authorization": f"Bearer {_token(SCOPE_SHELL_READ)}"}
    local_full = {
        "Authorization": f"Bearer {_token(f'{SCOPE_SHELL_READ} {SCOPE_SHELL_WRITE}')}"
    }
    remote_read = {
        "Authorization": f"Bearer {_token(f'{SCOPE_SHELL_READ} {SCOPE_REMOTE_USE}')}"
    }
    remote_full = {
        "Authorization": f"Bearer {_token(f'{SCOPE_SHELL_READ} {SCOPE_SHELL_WRITE} {SCOPE_REMOTE_USE}')}"
    }

    assert (
        client.get(
            "/api/ui/todos", params=local_params, headers=read_only
        ).status_code
        == 200
    )
    missing_local_write = client.put(
        "/api/ui/todos",
        json={**local_params, "expected_revision": 0, "todos": []},
        headers=read_only,
    )
    missing_remote = client.get(
        "/api/ui/todos", params=remote_params, headers=read_only
    )
    readable_remote = client.get(
        "/api/ui/todos", params=remote_params, headers=remote_read
    )
    missing_remote_write = client.put(
        "/api/ui/todos",
        json={**remote_params, "expected_revision": 0, "todos": []},
        headers=remote_read,
    )
    writable_local = client.put(
        "/api/ui/todos",
        json={**local_params, "expected_revision": 0, "todos": []},
        headers=local_full,
    )
    writable_remote = client.put(
        "/api/ui/todos",
        json={**remote_params, "expected_revision": 0, "todos": []},
        headers=remote_full,
    )

    assert missing_local_write.status_code == 403
    assert SCOPE_SHELL_WRITE in missing_local_write.text
    assert missing_remote.status_code == 403
    assert SCOPE_REMOTE_USE in missing_remote.text
    assert readable_remote.status_code == 200
    assert missing_remote_write.status_code == 403
    assert SCOPE_SHELL_WRITE in missing_remote_write.text
    assert writable_local.status_code == 200
    assert writable_remote.status_code == 200
