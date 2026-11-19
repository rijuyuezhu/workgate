"""Real browser: Task reuse, focused controls, and explicit Session+Task closure."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(180),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_task_reuse_and_explicit_finish_after_last_session(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    result = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "closure-ux-test"},
    )
    assert result["status"] == 200 and not result["payload"].get("isError")
    task_id = result["payload"]["structuredContent"]["result"]["task_id"]

    page = harness.page
    page.goto(f"{harness.base_url}/ui?task_id={task_id}#tasks")
    expect(page.locator("#task-status")).to_have_text("active")
    page.locator("#task-tab-sessions").click()
    page.locator("#task-session-start-open").click()
    expect(page.locator("#task-session-start-form")).to_be_visible()
    expect(
        page.locator(
            f'#task-session-executor option[value="{harness.executor_id}"]'
        )
    ).to_have_count(1)
    page.locator("#task-session-executor").select_option(harness.executor_id)
    page.locator("#task-session-workdir").fill(".")
    page.locator("#task-session-start-form button[type=submit]").click()
    expect(page.locator("#session-list .session-entry")).to_have_count(1)
    expect(page.locator("#session-detail-id")).not_to_have_text("—")
    expect(page.locator("#session-finish-task")).to_be_enabled()
    assert page.locator("#task-status").inner_text() == "active"

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#session-finish-task").click()
    expect(page.locator("#task-status")).to_have_text(
        "completed", timeout=15000
    )
    page.locator("#tasks-filter").select_option("completed")
    expect(
        page.locator(f'#task-list [data-task-id="{task_id}"]')
    ).to_be_visible()

    # Reopening preserves the Task identity; ending a Session alone puts it
    # available for later work, without falsely completing it.
    page.locator("#task-tab-progress").click()
    page.locator('[data-task-action="resume"]').click()
    expect(page.locator("#task-status")).to_have_text("active")
    page.locator("#task-tab-sessions").click()
    page.locator("#task-session-start-open").click()
    page.locator("#task-session-executor").select_option(harness.executor_id)
    page.locator("#task-session-workdir").fill(".")
    page.locator("#task-session-start-form button[type=submit]").click()
    expect(page.locator("#session-list .session-entry")).to_have_count(2)
    expect(page.locator("#session-terminate")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator(".session-end-options summary").click()
    page.locator("#session-terminate").click()
    expect(page.locator("#session-terminate")).to_be_disabled()
    expect(page.locator("#task-status")).to_have_text("active")
    page.locator("#tasks-filter").select_option("active")
    expect(
        page.locator(f'#task-list [data-task-id="{task_id}"]')
    ).to_be_visible()
    page.reload()
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#task-status")).to_have_text("active")


def test_finish_work_waits_for_other_live_session(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    created = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "two-live-sessions"},
    )
    assert created["status"] == 200, created
    task_id = created["payload"]["structuredContent"]["result"]["task_id"]
    session_ids = []
    for _ in range(2):
        started = harness.api(
            "POST",
            "/api/ui/sessions/start",
            body={
                "task_id": task_id,
                "executor_id": harness.executor_id,
                "workdir": ".",
            },
        )
        assert started["status"] == 200, started
        session_ids.append(started["payload"]["data"]["session"]["session_id"])

    page = harness.page
    page.goto(f"{harness.base_url}/ui?task_id={task_id}#tasks")
    expect(page.locator("#task-status")).to_have_text("active")
    page.locator("#task-tab-sessions").click()
    expect(page.locator("#session-list .session-entry")).to_have_count(2)
    page.locator(
        f'#session-list .session-entry[data-session-id="{session_ids[0]}"]'
    ).click()
    expect(page.locator("#session-finish-task")).to_be_disabled()

    # End one execution context without completing the shared Task.
    page.locator(".session-end-options summary").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#session-terminate").click()
    expect(page.locator("#task-status")).to_have_text("active")
    page.locator(
        f'#session-list .session-entry[data-session-id="{session_ids[1]}"]'
    ).click()
    expect(page.locator("#session-finish-task")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#session-finish-task").click()
    expect(page.locator("#task-status")).to_have_text("completed")

    detail = harness.api("GET", f"/api/ui/tasks/detail?task_id={task_id}")
    assert detail["status"] == 200, detail
    sessions = detail["payload"]["data"]["task"]["sessions"]
    assert {row["session_id"] for row in sessions} == set(session_ids)
    assert all(row["status"] == "ended" for row in sessions)


def test_single_task_finish_guard_without_batch_toolbar(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    ids = []
    for label in ("review-done", "review-unfinished", "review-restarted"):
        created = harness.api(
            "POST", "/tools/task", body={"action": "create", "label": label}
        )
        assert created["status"] == 200, created
        task_id = created["payload"]["structuredContent"]["result"]["task_id"]
        ids.append(task_id)
        started = harness.api(
            "POST",
            "/api/ui/sessions/start",
            body={
                "task_id": task_id,
                "executor_id": harness.executor_id,
                "workdir": ".",
            },
        )
        assert started["status"] == 200, started
        ended = harness.api(
            "POST",
            "/api/ui/sessions/terminate",
            body={
                "session_id": started["payload"]["data"]["session"][
                    "session_id"
                ]
            },
        )
        assert ended["status"] == 200, ended

    plan = harness.api(
        "PUT",
        "/api/ui/todos",
        body={
            "task_id": ids[1],
            "todos": [
                {
                    "id": "one",
                    "content": "Still pending",
                    "status": "pending",
                    "priority": "medium",
                }
            ],
        },
    )
    assert plan["status"] == 200, plan

    page = harness.page
    page.goto(f"{harness.base_url}/ui?task_id={ids[0]}#tasks")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#tasks-filter option[value=ready]")).to_have_count(0)
    expect(page.locator("#task-list input[type=checkbox]")).to_have_count(0)
    expect(page.locator("#tasks-complete-selected")).to_have_count(0)
    expect(page.locator("#task-status")).to_have_text("active")
    page.screenshot(path=str(harness.artifacts / "simple-task-list.png"))

    # Other agents may start working again while an operator reviews a Task.
    restarted = harness.api(
        "POST",
        "/api/ui/sessions/start",
        body={
            "task_id": ids[2],
            "executor_id": harness.executor_id,
            "workdir": ".",
        },
    )
    assert restarted["status"] == 200, restarted

    def guarded_finish(task_id: str) -> dict:
        return harness.api(
            "POST",
            "/api/ui/tasks/finish",
            body={"task_id": task_id, "require_no_live_sessions": True},
        )

    assert guarded_finish(ids[0])["status"] == 200
    assert guarded_finish(ids[1])["status"] == 400
    assert guarded_finish(ids[2])["status"] == 400
    for task_id, expected_status in zip(
        ids, ("completed", "active", "active"), strict=True
    ):
        detail = harness.api("GET", f"/api/ui/tasks/detail?task_id={task_id}")
        assert detail["payload"]["data"]["task"]["status"] == expected_status
