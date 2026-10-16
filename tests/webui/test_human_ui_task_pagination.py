"""Bounded, owner-checked Task and legacy Session WebUI browsing."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.oauth.core.scopes import SCOPE_SHELL_READ
from workgate.oauth.protocol.token_codec import issue_access_token

BASE = "https://workgate.example"


def setup(monkeypatch, tmp_path, *, page_size=3, auth_mode="none"):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_UI_TASK_PAGE_SIZE", str(page_size))
    clear_settings_cache()
    app, harness = build_paired_http_app(get_settings())
    return TestClient(app, base_url=BASE), harness


@pytest.mark.asyncio
async def test_status_cursor_default_limit_and_detail(tmp_path, monkeypatch):
    client, harness = setup(monkeypatch, tmp_path, page_size=2)
    service = harness.control.task_service
    task_ids = []
    for n in range(7):
        task = await service.create_task(label=f"Task {n}")
        task_ids.append(task.task_id)
    await service.block_task(task_ids[1])
    await service.finish_task(task_ids[2])
    await service.cancel_task(task_ids[3])

    response = client.get("/api/ui/tasks")
    assert response.status_code == 200, response.text
    first = response.json()["data"]
    assert first["status"] == "active"
    assert first["total"] == 4
    assert first["active_count"] == 4
    assert len(first["tasks"]) == 2
    assert first["next_cursor"]
    assert all(t["status"] == "active" for t in first["tasks"])
    assert all(
        "plan" not in t
        and "progress" not in t
        and "session_ids" not in t
        and "sessions" not in t
        for t in first["tasks"]
    )
    second = client.get(
        "/api/ui/tasks", params={"cursor": first["next_cursor"]}
    ).json()["data"]
    assert len(second["tasks"]) == 2
    assert second["next_cursor"] is None
    assert not (
        {t["task_id"] for t in first["tasks"]}
        & {t["task_id"] for t in second["tasks"]}
    )
    assert (
        client.get("/api/ui/tasks", params={"status": "blocked"}).json()[
            "data"
        ]["total"]
        == 1
    )
    assert (
        client.get("/api/ui/tasks", params={"status": "completed"}).json()[
            "data"
        ]["total"]
        == 1
    )
    assert (
        client.get("/api/ui/tasks", params={"status": "cancelled"}).json()[
            "data"
        ]["total"]
        == 1
    )
    assert (
        client.get(
            "/api/ui/tasks", params={"status": "all", "limit": 100}
        ).json()["data"]["total"]
        == 7
    )
    detail = client.get("/api/ui/tasks/detail", params={"task_id": task_ids[2]})
    assert detail.status_code == 200
    assert detail.json()["data"]["task"]["task_id"] == task_ids[2]
    assert "plan" in detail.json()["data"]["task"]
    assert (
        client.get(
            "/api/ui/tasks/detail", params={"task_id": "task_missing"}
        ).status_code
        == 404
    )

    # Cursor is a keyset position. Insert/update rows at the front; no duplicate
    # old-page rows, and the cursor remains bound to its original filter.
    await service.create_task(label="New at top")
    follow = client.get(
        "/api/ui/tasks", params={"cursor": first["next_cursor"]}
    ).json()["data"]
    assert not (
        {t["task_id"] for t in first["tasks"]}
        & {t["task_id"] for t in follow["tasks"]}
    )


@pytest.mark.asyncio
async def test_cursor_input_and_page_size_validation(tmp_path, monkeypatch):
    client, _harness = setup(monkeypatch, tmp_path)
    for params in (
        {"status": "bogus"},
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "abc"},
        {"limit": "-1"},
        {"cursor": "***"},
        {"cursor": "A" * 513},
    ):
        response = client.get("/api/ui/tasks", params=params)
        assert response.status_code == 400, (params, response.text)
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"status": "completed", "key": [1, 2, "task_any"]}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    assert (
        client.get(
            "/api/ui/tasks", params={"status": "active", "cursor": cursor}
        ).status_code
        == 400
    )
    assert client.get("/api/ui/tasks/detail").status_code == 400
    assert (
        client.get(
            "/api/ui/tasks/detail", params={"task_id": "a", "session_id": "b"}
        ).status_code
        == 400
    )
    assert get_settings().ui_task_page_size == 3


@pytest.mark.asyncio
async def test_unattached_paging_and_deep_link_reachability(
    tmp_path, monkeypatch
):
    client, harness = setup(monkeypatch, tmp_path, page_size=1)
    (tmp_path / "project").mkdir()
    task = await harness.control.task_service.create_task(label="attached")
    attached = await harness.control.session_coordinator.start_session(
        workdir="project", executor_id=harness.executor_id, task_id=task.task_id
    )
    one = await harness.control.session_coordinator.start_session(
        workdir="project", executor_id=harness.executor_id, label="legacy-one"
    )
    two = await harness.control.session_coordinator.start_session(
        workdir="project", executor_id=harness.executor_id, label="legacy-two"
    )
    assert (
        isinstance(one, dict)
        and isinstance(two, dict)
        and isinstance(attached, dict)
    )
    one_id = one.get("session_id")
    two_id = two.get("session_id")
    attached_id = attached.get("session_id")
    assert (
        isinstance(one_id, str)
        and isinstance(two_id, str)
        and isinstance(attached_id, str)
    )

    service = harness.control.task_service
    for result in (one, two):
        implicit_id = str(result["task_id"])
        await service.cancel_task(implicit_id)
        await service.delete_task(implicit_id)
    default = client.get("/api/ui/tasks").json()["data"]
    assert default["unattached_sessions"] == []
    first = client.get("/api/ui/tasks", params={"status": "unattached"}).json()[
        "data"
    ]
    assert first["total"] == 2
    assert len(first["unattached_sessions"]) == 1
    second = client.get(
        "/api/ui/tasks",
        params={"status": "unattached", "cursor": first["next_cursor"]},
    ).json()["data"]
    ids = [
        first["unattached_sessions"][0]["session_id"],
        second["unattached_sessions"][0]["session_id"],
    ]
    assert len(set(ids)) == 2
    assert second["next_cursor"] is None
    old = client.get(
        "/api/ui/tasks/detail", params={"session_id": one_id}
    ).json()["data"]
    assert old["session"]["session_id"] == one_id
    assert old["task"] is None
    linked = client.get(
        "/api/ui/tasks/detail", params={"session_id": attached_id}
    ).json()["data"]
    assert linked["task"]["task_id"] == task.task_id
    assert linked["task"]["sessions"][0]["session_id"] == attached_id
    assert (
        client.get(
            "/api/ui/tasks",
            params={
                "status": "unattached",
                "cursor": first["next_cursor"],
                "limit": 101,
            },
        ).status_code
        == 400
    )


@pytest.mark.asyncio
async def test_paged_task_owner_isolation(tmp_path, monkeypatch):
    client, harness = setup(monkeypatch, tmp_path, auth_mode="oauth")

    def auth(subject):
        token = issue_access_token(
            client_id="task-page-test",
            scope=SCOPE_SHELL_READ,
            resource=f"{BASE}/mcp",
            subject=subject,
        )
        return {"Authorization": f"Bearer {token}"}

    # Tasks created without OAuth claims belong to the local principal.
    owner = await harness.control.task_service.create_task(label="private")
    other = auth("other-user")
    assert (
        client.get("/api/ui/tasks", headers=other).json()["data"]["tasks"] == []
    )
    assert (
        client.get(
            "/api/ui/tasks/detail",
            params={"task_id": owner.task_id},
            headers=other,
        ).status_code
        == 403
    )
    assert client.get("/api/ui/tasks").status_code == 401


@pytest.mark.asyncio
async def test_large_history_default_response_stays_bounded(
    tmp_path, monkeypatch
):
    client, harness = setup(monkeypatch, tmp_path, page_size=50)
    for index in range(55):
        await harness.control.task_service.create_task(
            label=f"large-history-{index}"
        )

    # No session activity work should occur when requesting plain summaries.
    async def unexpected_activity(_session_id):
        raise AssertionError("default page must not project session activity")

    monkeypatch.setattr(
        harness.control.session_coordinator,
        "session_activity_projection",
        unexpected_activity,
    )
    first_response = client.get("/api/ui/tasks")
    assert first_response.status_code == 200
    first = first_response.json()["data"]
    assert first["total"] == 55
    assert len(first["tasks"]) == 50
    assert all(len(row) == 7 for row in first["tasks"])
    assert len(first_response.content) < 25_000
    second = client.get(
        "/api/ui/tasks", params={"cursor": first["next_cursor"]}
    ).json()["data"]
    assert len(second["tasks"]) == 5
    assert second["next_cursor"] is None
    assert (
        len({row["task_id"] for row in first["tasks"] + second["tasks"]}) == 55
    )
