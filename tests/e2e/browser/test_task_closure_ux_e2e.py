"""Real browser: Task reuse, ready inbox, and explicit Session+Task closure."""

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
    # in the ready-to-close inbox, but never auto-completes it.
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
    page.locator("#session-terminate").click()
    expect(page.locator("#session-terminate")).to_be_disabled()
    expect(page.locator("#task-status")).to_have_text("active")
    page.locator("#tasks-filter").select_option("ready")
    expect(
        page.locator(f'#task-list [data-task-id="{task_id}"]')
    ).to_be_visible()
    page.reload()
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#task-status")).to_have_text("active")
