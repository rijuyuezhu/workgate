"""Exercise human Task lifecycle controls against the real Control service."""

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def run_task_lifecycle_ui(harness: BrowserHarness) -> None:
    page = harness.page
    created = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "lifecycle-browser"},
    )
    assert created["status"] == 200
    task_id = created["payload"]["structuredContent"]["result"]["task_id"]
    harness.navigate("tasks")
    page.locator("#tasks-refresh").click()
    entry = page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    expect(entry).to_be_visible()
    entry.click()
    finish = page.locator('[data-task-action="finish"]')
    reopen = page.locator('[data-task-action="resume"]')
    block = page.locator('[data-task-action="block"]')
    cancel = page.locator('[data-task-action="cancel"]')
    delete = page.locator('[data-task-action="delete"]')
    expect(page.locator("#task-status")).to_have_text("active")
    expect(reopen).to_be_disabled()
    expect(delete).to_be_disabled()
    expect(block).to_be_enabled()
    page.screenshot(
        path=str(harness.artifacts / "task-lifecycle-actions-desktop.png")
    )
    block.click()
    expect(page.locator("#task-status")).to_have_text("blocked")
    expect(page.locator("#task-action-state")).to_contain_text("Task blocked")
    expect(block).to_be_disabled()
    expect(reopen).to_be_enabled()
    expect(reopen).to_have_text("Resume")
    reopen.click()
    expect(page.locator("#task-status")).to_have_text("active")
    finish.click()
    expect(page.locator("#task-status")).to_have_text("completed")
    expect(delete).to_be_enabled()
    expect(cancel).to_be_disabled()
    expect(reopen).to_have_text("Reopen")
    reopen.click()
    expect(page.locator("#task-status")).to_have_text("active")
    page.once("dialog", lambda dialog: dialog.dismiss())
    cancel.click()
    expect(page.locator("#task-status")).to_have_text("active")
    page.once("dialog", lambda dialog: dialog.accept())
    cancel.click()
    expect(page.locator("#task-status")).to_have_text("cancelled")
    expect(reopen).to_be_disabled()
    expect(delete).to_be_enabled()
    # Mobile deletion should return to the task list, not a blank detail page.
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator(".tasks-workspace")).to_be_visible()
    expect(delete).to_be_visible()
    page.screenshot(
        path=str(harness.artifacts / "task-lifecycle-actions-mobile.png")
    )
    page.once("dialog", lambda dialog: dialog.dismiss())
    delete.click()
    expect(page.locator("#task-status")).to_have_text("cancelled")
    page.once("dialog", lambda dialog: dialog.accept())
    delete.click()
    expect(entry).to_have_count(0)
    expect(page.locator("#tasks-panel")).to_have_attribute(
        "data-mobile-step", "list"
    )
    expect(page.locator("#task-list")).to_be_visible()
    page.set_viewport_size({"width": 1360, "height": 900})
    expect(page.locator("#task-state")).to_contain_text("Select a task")
    expect(page.locator("#task-list .task-entry").first).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
