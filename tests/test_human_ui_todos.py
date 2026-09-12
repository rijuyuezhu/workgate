from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.helpers import build_paired_http_app
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.todos import TodoConflictError
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


async def _client_with_session(
    monkeypatch, tmp_path, *, auth_mode: str = "none"
):
    _configure(monkeypatch, tmp_path, auth_mode=auth_mode)
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    app, harness = build_paired_http_app(get_settings())
    started = await harness.control.session_coordinator.start_session(
        workdir="project", label="todos", executor_id=harness.executor_id
    )
    assert isinstance(started, dict)
    client = TestClient(app, base_url=BASE_URL, client=("203.0.113.14", 50005))
    return client, harness, str(started["session_id"])


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
async def test_todos_require_explicit_final_shared_session_and_return_metadata(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )

    missing = client.get("/api/ui/todos")
    malformed = client.get("/api/ui/todos", params={"session_id": "LOCAL0001"})
    initial = client.get("/api/ui/todos", params={"session_id": session_id})

    assert missing.status_code == 400
    assert malformed.status_code == 400
    assert initial.status_code == 200
    data = initial.json()["data"]
    assert data["session_id"] == session_id
    assert data["session"]["session_id"] == session_id
    assert data["session"]["executor_id"] == harness.executor_id
    assert data["session"]["label"] == "todos"
    assert data["revision"] == 0
    assert data["todos"] == []
    assert data["limits"]["todos"] == get_settings().max_todos


@pytest.mark.asyncio
async def test_todos_write_read_and_stale_revision_conflict(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    body = {
        "session_id": session_id,
        "expected_revision": 0,
        "todos": [_todo("one", "first")],
    }

    saved = client.put("/api/ui/todos", json=body)
    stale = client.put("/api/ui/todos", json=body)
    current = client.get("/api/ui/todos", params={"session_id": session_id})

    assert saved.status_code == 200
    assert saved.json()["data"]["revision"] == 1
    assert stale.status_code == 409
    assert stale.json()["error"] == "TodoConflictError"
    assert current.status_code == 200
    assert current.json()["data"]["revision"] == 1
    assert current.json()["data"]["todos"][0]["content"] == "first"


@pytest.mark.asyncio
async def test_todos_are_isolated_by_shared_session(tmp_path, monkeypatch):
    client, harness, first_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    (tmp_path / "second").mkdir()
    second = await harness.control.session_coordinator.start_session(
        workdir="second", executor_id=harness.executor_id
    )
    assert isinstance(second, dict)
    second_id = str(second["session_id"])

    saved = client.put(
        "/api/ui/todos",
        json={
            "session_id": first_id,
            "expected_revision": 0,
            "todos": [_todo("first")],
        },
    )
    second_state = client.get("/api/ui/todos", params={"session_id": second_id})

    assert saved.status_code == 200
    assert second_state.status_code == 200
    assert second_state.json()["data"]["revision"] == 0
    assert second_state.json()["data"]["todos"] == []


@pytest.mark.asyncio
async def test_todo_http_validates_shape_count_ids_and_encoded_lengths(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    base = {"session_id": session_id, "expected_revision": 0}

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
            {"session_id": session_id, "expected_revision": True, "todos": []},
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
async def test_todo_service_revision_guard_serializes_concurrent_replacements(
    tmp_path, monkeypatch
):
    _client, harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service

    results = await asyncio.gather(
        service.write(session_id, [_todo("a")], 0),
        service.write(session_id, [_todo("b")], 0),
        return_exceptions=True,
    )

    successes = [
        item for item in results if not isinstance(item, BaseException)
    ]
    conflicts = [
        item for item in results if isinstance(item, TodoConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    current = await service.read(session_id)
    assert current.revision == 1
    assert current.todos[0].id in {"a", "b"}


@pytest.mark.asyncio
async def test_todos_become_inaccessible_after_session_end(
    tmp_path, monkeypatch
):
    client, harness, session_id = await _client_with_session(
        monkeypatch, tmp_path
    )
    saved = client.put(
        "/api/ui/todos",
        json={
            "session_id": session_id,
            "expected_revision": 0,
            "todos": [_todo("one")],
        },
    )
    assert saved.status_code == 200
    await harness.control.session_coordinator.end_session(session_id)

    response = client.get("/api/ui/todos", params={"session_id": session_id})

    assert response.status_code == 400
    assert "inactive session_id" in response.text


@pytest.mark.asyncio
async def test_todo_oauth_scopes_are_shared_session_scopes_only(
    tmp_path, monkeypatch
):
    client, _harness, session_id = await _client_with_session(
        monkeypatch, tmp_path, auth_mode="oauth"
    )
    read = _headers(SCOPE_SHELL_READ)
    write = _headers(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_WRITE}")

    readable = client.get(
        "/api/ui/todos", params={"session_id": session_id}, headers=read
    )
    denied_write = client.put(
        "/api/ui/todos",
        json={"session_id": session_id, "expected_revision": 0, "todos": []},
        headers=read,
    )
    writable = client.put(
        "/api/ui/todos",
        json={"session_id": session_id, "expected_revision": 0, "todos": []},
        headers=write,
    )

    assert readable.status_code == 200
    assert denied_write.status_code == 403
    assert SCOPE_SHELL_WRITE in denied_write.text
    assert writable.status_code == 200
