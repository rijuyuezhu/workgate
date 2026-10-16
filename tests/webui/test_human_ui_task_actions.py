"""Task lifecycle WebUI API reuses the authoritative state machine."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.oauth.core.scopes import SCOPE_SHELL_READ, SCOPE_SHELL_WRITE
from workgate.oauth.protocol.token_codec import issue_access_token

BASE_URL = "https://workgate.example"


def _configure(monkeypatch, tmp_path, auth_mode="none"):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    clear_settings_cache()


def _post(client, action, task_id, *, headers=None):
    return client.post(
        f"/api/ui/tasks/{action}",
        json={"task_id": str(task_id)},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_task_actions_follow_status_machine_and_delete(
    tmp_path, monkeypatch
):
    _configure(monkeypatch, tmp_path)
    app, harness = build_paired_http_app(get_settings())
    service = harness.control.task_service
    task = await service.create_task(label="lifecycle")
    client = TestClient(app, base_url=BASE_URL)

    assert (
        _post(client, "block", task.task_id).json()["data"]["task"]["status"]
        == "blocked"
    )
    invalid = _post(client, "block", task.task_id)
    assert invalid.status_code == 400
    assert "cannot block" in invalid.text
    assert (
        _post(client, "resume", task.task_id).json()["data"]["task"]["status"]
        == "active"
    )
    assert (
        _post(client, "finish", task.task_id).json()["data"]["task"]["status"]
        == "completed"
    )
    assert (
        _post(client, "resume", task.task_id).json()["data"]["task"]["status"]
        == "active"
    )
    assert (
        _post(client, "cancel", task.task_id).json()["data"]["task"]["status"]
        == "cancelled"
    )
    assert _post(client, "resume", task.task_id).status_code == 400
    deleted = _post(client, "delete", task.task_id)
    assert deleted.status_code == 200
    assert deleted.json()["data"]["task"] is None
    assert _post(client, "delete", task.task_id).status_code == 400
    assert not [
        task
        for task in await service.list_tasks()
        if task.task_id == deleted.json()["data"]["task_id"]
    ]


@pytest.mark.asyncio
async def test_task_actions_require_completed_plan_and_valid_delete(
    tmp_path, monkeypatch
):
    _configure(monkeypatch, tmp_path)
    app, harness = build_paired_http_app(get_settings())
    service = harness.control.task_service
    task = await service.create_task(label="with plan")
    client = TestClient(app, base_url=BASE_URL)
    await service.update_plan(
        str(task.task_id),
        steps=[{"id": "one", "content": "step", "status": "pending"}],
    )
    assert "unfinished plan steps" in _post(client, "finish", task.task_id).text
    assert (
        "only completed or cancelled"
        in _post(client, "delete", task.task_id).text
    )
    await service.update_plan(
        str(task.task_id),
        steps=[{"id": "one", "content": "step", "status": "completed"}],
    )
    assert _post(client, "finish", task.task_id).status_code == 200
    assert _post(client, "delete", task.task_id).status_code == 200


@pytest.mark.asyncio
async def test_task_actions_validate_payload_and_auth(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path, "oauth")
    app, harness = build_paired_http_app(get_settings())
    client = TestClient(app, base_url=BASE_URL)
    task = await harness.control.task_service.create_task(label="private")

    def auth(scope):
        token = issue_access_token(
            client_id="ui-task-action-test",
            scope=scope,
            resource=f"{BASE_URL}/mcp",
        )
        return {"Authorization": f"Bearer {token}"}

    read = auth(SCOPE_SHELL_READ)
    write = auth(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_WRITE}")
    assert _post(client, "block", task.task_id).status_code == 401
    assert _post(client, "block", task.task_id, headers=read).status_code == 403
    assert (
        _post(client, "block", task.task_id, headers=write).status_code == 200
    )
    for body in (
        {},
        {"task_id": "oops"},
        {"task_id": task.task_id, "extra": 1},
        [],
    ):
        resp = client.post("/api/ui/tasks/resume", json=body, headers=write)
        assert resp.status_code == 400, (body, resp.text)
    assert (
        _post(client, "bogus", task.task_id, headers=write).status_code == 404
    )

    other_token = issue_access_token(
        client_id="other-user",
        scope=f"{SCOPE_SHELL_READ} {SCOPE_SHELL_WRITE}",
        resource=f"{BASE_URL}/mcp",
        subject="another-principal",
    )
    other = {"Authorization": f"Bearer {other_token}"}
    assert (
        _post(client, "resume", task.task_id, headers=other).status_code == 403
    )
    assert (
        _post(client, "resume", task.task_id, headers=write).status_code == 200
    )


@pytest.mark.asyncio
async def test_task_actions_concurrent_update_is_serialized(
    tmp_path, monkeypatch
):
    _configure(monkeypatch, tmp_path)
    app, harness = build_paired_http_app(get_settings())
    task = await harness.control.task_service.create_task(label="race")
    # The HTTP adapter calls the transaction-serialized Task service; concurrent
    # calls must not both silently succeed for the same starting state.
    with TestClient(app, base_url=BASE_URL) as client:
        first, second = await asyncio.gather(
            asyncio.to_thread(_post, client, "block", task.task_id),
            asyncio.to_thread(_post, client, "cancel", task.task_id),
        )
    codes = sorted([first.status_code, second.status_code])
    assert codes in ([200, 200], [200, 400]), codes
    state = (await harness.control.task_service.list_tasks())[0].status
    assert state in {"blocked", "cancelled"}


@pytest.mark.asyncio
async def test_task_action_audit_is_emitted_by_canonical_service(
    tmp_path, monkeypatch
):
    _configure(monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(
        "workgate.control.task_state.audit",
        lambda event, **fields: events.append((event, fields)),
    )
    app, harness = build_paired_http_app(get_settings())
    task = await harness.control.task_service.create_task(label="audit")
    with TestClient(app, base_url=BASE_URL) as client:
        for action in ("block", "resume", "finish", "delete"):
            assert _post(client, action, task.task_id).status_code == 200
    recorded = [
        fields["operation"]
        for event, fields in events
        if event == "task_mutation"
    ]
    assert recorded[-3:] == ["block", "resume", "finish"]
    assert any(event == "task_deleted" for event, _ in events)


@pytest.mark.asyncio
async def test_deleting_task_detaches_but_does_not_end_retained_session(
    tmp_path, monkeypatch
):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "project").mkdir()
    app, harness = build_paired_http_app(get_settings())
    task = await harness.control.task_service.create_task(label="keep session")
    session = await harness.control.session_coordinator.start_session(
        workdir="project",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    assert isinstance(session, dict)
    session_id = session.get("session_id")
    assert isinstance(session_id, str)
    with TestClient(app, base_url=BASE_URL) as client:
        assert _post(client, "cancel", task.task_id).status_code == 200
        retained = harness.control.control_state.snapshot_sessions()[session_id]
        assert retained.task_id == task.task_id
        assert retained.status != "ended"
        assert _post(client, "delete", task.task_id).status_code == 200
        detached = harness.control.control_state.snapshot_sessions()[session_id]
        assert detached.task_id is None
        assert detached.status != "ended"
        inventory = client.get("/api/ui/tasks", params={"status": "unattached"})
        assert inventory.status_code == 200
        assert session_id in {
            row["session_id"]
            for row in inventory.json()["data"]["unattached_sessions"]
        }
