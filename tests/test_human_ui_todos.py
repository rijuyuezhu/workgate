import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.helpers import build_paired_http_app
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.task_state import TaskRevisionConflictError
from workgate.oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from workgate.oauth.protocol.token_codec import issue_access_token

BASE_URL = "https://workgate.example"


def _configure(monkeypatch, tmp_path, *, auth_mode: str = "none") -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    clear_settings_cache()


async def _client_with_task(monkeypatch, tmp_path, *, auth_mode: str = "none"):
    _configure(monkeypatch, tmp_path, auth_mode=auth_mode)
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    app, harness = build_paired_http_app(get_settings())
    task = await harness.control.task_service.create_task(label="todos")
    started = await harness.control.session_coordinator.start_session(
        workdir="project",
        label="todos",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    assert isinstance(started, dict)
    client = TestClient(app, base_url=BASE_URL, client=("203.0.113.14", 50005))
    return client, harness, task.task_id, str(started["session_id"])


def _todo(identifier: str, content: str = "task") -> dict[str, str]:
    return {
        "id": identifier,
        "content": content,
        "status": "pending",
        "priority": "medium",
    }


def _token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-todos-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def _headers(scope: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(scope)}"}


@pytest.mark.asyncio
async def test_todos_require_explicit_task_and_return_metadata(
    tmp_path, monkeypatch
):
    client, _harness, task_id, session_id = await _client_with_task(
        monkeypatch, tmp_path
    )

    missing = client.get("/api/ui/todos")
    malformed = client.get("/api/ui/todos", params={"task_id": "LOCAL0001"})
    initial = client.get("/api/ui/todos", params={"task_id": task_id})

    assert missing.status_code == 400
    assert malformed.status_code == 400
    assert initial.status_code == 200
    data = initial.json()["data"]
    assert data["task_id"] == task_id
    assert data["task"]["task_id"] == task_id
    assert data["task"]["session_ids"] == [session_id]
    assert data["task"]["label"] == "todos"
    assert data["revision"] == 0
    assert data["todos"] == []
    assert data["limits"]["todos"] == get_settings().max_todos


@pytest.mark.asyncio
async def test_todos_write_read_and_stale_revision_conflict(
    tmp_path, monkeypatch
):
    client, _harness, task_id, _session_id = await _client_with_task(
        monkeypatch, tmp_path
    )
    body = {
        "task_id": task_id,
        "expected_revision": 0,
        "todos": [_todo("one", "first")],
    }

    saved = client.put("/api/ui/todos", json=body)
    stale = client.put("/api/ui/todos", json=body)
    current = client.get("/api/ui/todos", params={"task_id": task_id})

    assert saved.status_code == 200
    assert saved.json()["data"]["revision"] == 1
    assert saved.json()["data"]["task"]["revision"] == 1
    assert stale.status_code == 409
    assert stale.json()["error"] == "revision_conflict"
    assert current.status_code == 200
    assert current.json()["data"]["revision"] == 1
    assert current.json()["data"]["todos"][0]["content"] == "first"


@pytest.mark.asyncio
async def test_todos_are_isolated_by_semantic_task(tmp_path, monkeypatch):
    client, harness, first_task_id, _session_id = await _client_with_task(
        monkeypatch, tmp_path
    )
    second = await harness.control.task_service.create_task(label="second")

    saved = client.put(
        "/api/ui/todos",
        json={
            "task_id": first_task_id,
            "expected_revision": 0,
            "todos": [_todo("first")],
        },
    )
    second_state = client.get(
        "/api/ui/todos", params={"task_id": second.task_id}
    )

    assert saved.status_code == 200
    assert second_state.status_code == 200
    assert second_state.json()["data"]["revision"] == 0
    assert second_state.json()["data"]["todos"] == []


@pytest.mark.asyncio
async def test_todo_http_validates_shape_count_ids_and_encoded_lengths(
    tmp_path, monkeypatch
):
    client, _harness, task_id, _session_id = await _client_with_task(
        monkeypatch, tmp_path
    )
    base = {"task_id": task_id, "expected_revision": 0}

    cases = [
        ({**base, "todos": {}}, "todos must be a JSON array"),
        (
            {
                **base,
                "todos": [_todo("same"), _todo("same")],
            },
            "duplicate todo id",
        ),
        (
            {**base, "todos": [_todo("one", "界" * 6000)]},
            "content exceeds",
        ),
        (
            {"task_id": task_id, "expected_revision": True, "todos": []},
            "non-negative integer",
        ),
    ]
    for payload, message in cases:
        response = client.put("/api/ui/todos", json=payload)
        assert response.status_code == 400
        assert message in response.text

    too_many = client.put(
        "/api/ui/todos",
        json={
            **base,
            "todos": [
                _todo(str(index))
                for index in range(get_settings().max_todos + 1)
            ],
        },
    )
    assert too_many.status_code == 400
    assert "max is" in too_many.text


@pytest.mark.asyncio
async def test_task_service_revision_guard_serializes_concurrent_replacements(
    tmp_path, monkeypatch
):
    _client, harness, task_id, _session_id = await _client_with_task(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service

    results = await asyncio.gather(
        service.write(task_id, [_todo("a")], 0),
        service.write(task_id, [_todo("b")], 0),
        return_exceptions=True,
    )

    successes = [
        item for item in results if not isinstance(item, BaseException)
    ]
    conflicts = [
        item for item in results if isinstance(item, TaskRevisionConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    current = await service.read(task_id)
    assert current.revision == 1
    assert current.todos[0].id in {"a", "b"}


@pytest.mark.asyncio
async def test_todos_remain_mutable_after_attached_session_ends(
    tmp_path, monkeypatch
):
    client, harness, task_id, session_id = await _client_with_task(
        monkeypatch, tmp_path
    )
    saved = client.put(
        "/api/ui/todos",
        json={
            "task_id": task_id,
            "expected_revision": 0,
            "todos": [_todo("one")],
        },
    )
    assert saved.status_code == 200
    await harness.control.session_coordinator.end_session(session_id)

    response = client.get("/api/ui/todos", params={"task_id": task_id})
    updated = client.put(
        "/api/ui/todos",
        json={
            "task_id": task_id,
            "expected_revision": 1,
            "todos": [_todo("two")],
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["todos"][0]["id"] == "one"
    assert response.json()["data"]["task"]["status"] == "active"
    assert response.json()["data"]["task"]["session_ids"] == [session_id]
    assert updated.status_code == 200
    assert updated.json()["data"]["todos"][0]["id"] == "two"


@pytest.mark.asyncio
async def test_todo_oauth_scopes_are_task_scopes_only(tmp_path, monkeypatch):
    client, _harness, task_id, _session_id = await _client_with_task(
        monkeypatch, tmp_path, auth_mode="oauth"
    )
    read = _headers(SCOPE_SHELL_READ)
    write = _headers(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_WRITE}")

    readable = client.get(
        "/api/ui/todos", params={"task_id": task_id}, headers=read
    )
    denied_write = client.put(
        "/api/ui/todos",
        json={"task_id": task_id, "expected_revision": 0, "todos": []},
        headers=read,
    )
    writable = client.put(
        "/api/ui/todos",
        json={"task_id": task_id, "expected_revision": 0, "todos": []},
        headers=write,
    )

    assert readable.status_code == 200
    assert denied_write.status_code == 403
    assert SCOPE_SHELL_WRITE in denied_write.text
    assert writable.status_code == 200
