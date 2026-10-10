"""Mobile lists navigate to full-width detail pages without affecting desktop UX."""

from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness


def run_mobile_drilldown(harness: BrowserHarness) -> None:
    page = harness.page
    page.set_viewport_size({"width": 390, "height": 844})

    def step(view: str, value: str) -> None:
        expect(page.locator(f'[data-app-view="{view}"]')).to_have_attribute(
            "data-mobile-step", value
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth + 1"
        ), (view, value)

    harness.navigate("tasks")
    step("tasks", "list")
    expect(page.locator("#task-list")).to_be_visible()
    expect(page.locator(".tasks-workspace")).to_be_hidden()
    task = (
        page.locator("#task-list .task-entry").filter(has_text="session").first
    )
    task.click()
    step("tasks", "task")
    expect(page.locator(".tasks-workspace")).to_be_visible()
    expect(page.locator("#task-list")).to_be_hidden()
    expect(
        page.locator(".mobile-subpage-bar:visible .mobile-subpage-back")
    ).to_have_text("← All tasks")
    page.screenshot(path=str(harness.artifacts / "mobile-task-detail.png"))

    page.locator("#task-tab-plan").click()
    expect(page.locator("#todo-list .todo-row").first).to_be_visible()
    expect(page.locator(".todo-row-heading").first).to_be_visible()
    expect(page.locator(".todo-row .todo-id").first).to_contain_text("Step")
    page.screenshot(path=str(harness.artifacts / "mobile-task-plan.png"))

    page.locator("#task-tab-sessions").click()
    expect(page.locator("#session-list .session-entry").first).to_be_visible()
    expect(page.locator(".task-sessions-main")).to_be_hidden()
    page.locator("#session-list .session-entry").first.click()
    step("tasks", "session")
    expect(page.locator("#session-list")).to_be_hidden()
    expect(page.locator("#session-audit-list")).to_be_visible()
    expect(
        page.locator(".mobile-subpage-bar:visible .mobile-subpage-back")
    ).to_have_text("← Task")
    expect(
        page.locator("#session-audit-list .audit-entry").first
    ).to_be_visible()
    page.screenshot(path=str(harness.artifacts / "mobile-session-audit.png"))

    page.locator("#session-audit-list .audit-entry").first.click()
    step("tasks", "session-record")
    expect(page.locator("#session-audit-list")).to_be_hidden()
    expect(page.locator("#session-audit-detail-body")).to_be_visible()
    page.screenshot(
        path=str(harness.artifacts / "mobile-session-audit-record.png")
    )

    page.go_back(wait_until="domcontentloaded")
    step("tasks", "session")
    page.locator(".mobile-subpage-bar:visible .mobile-subpage-back").click()
    step("tasks", "task")
    page.locator(".mobile-subpage-bar:visible .mobile-subpage-back").click()
    step("tasks", "list")

    harness.navigate("executors")
    step("executors", "list")
    expect(page.locator(".executor-details")).to_be_hidden()
    page.locator("#executor-list .executor-row").first.click()
    step("executors", "detail")
    expect(page.locator(".executor-details")).to_be_visible()
    page.screenshot(path=str(harness.artifacts / "mobile-executor-detail.png"))
    page.locator(".mobile-subpage-bar:visible .mobile-subpage-back").click()
    step("executors", "list")

    harness.navigate("files")
    step("files", "list")
    expect(page.locator(".file-preview")).to_be_hidden()
    page.locator("#file-list .file-entry").first.click()
    step("files", "detail")
    expect(page.locator(".file-preview")).to_be_visible()
    page.screenshot(path=str(harness.artifacts / "mobile-file-preview.png"))
    page.locator(".mobile-subpage-bar:visible .mobile-subpage-back").click()
    step("files", "list")

    harness.navigate("audit")
    step("audit", "list")
    page.locator("#audit-time").select_option("0")
    page.locator("#audit-refresh").click()
    expect(page.locator("#audit-list .audit-entry").first).to_be_visible()
    page.locator("#audit-list .audit-entry").first.click()
    step("audit", "detail")
    expect(page.locator("#audit-list")).to_be_hidden()
    expect(page.locator("#audit-detail-body")).to_be_visible()
    page.screenshot(
        path=str(harness.artifacts / "mobile-global-audit-record.png")
    )
    page.locator(".mobile-subpage-bar:visible .mobile-subpage-back").click()
    step("audit", "list")

    # Responsive state changes must restore the desktop two-pane layout.
    page.set_viewport_size({"width": 1440, "height": 900})
    harness.navigate("tasks")
    expect(page.locator(".mobile-subpage-bar:visible")).to_have_count(0)
    expect(page.locator("#task-list")).to_be_visible()
    expect(page.locator(".tasks-workspace")).to_be_visible()

    # Branding is a real Home control, including from a nested mobile page.
    page.set_viewport_size({"width": 390, "height": 844})
    harness.navigate("audit")
    page.locator("#audit-list .audit-entry").first.click()
    step("audit", "detail")
    page.locator(".brand-block").click()
    expect(page.locator("#page-title")).to_have_text("Overview")
    expect(page.locator('.nav-item[data-view="overview"]')).to_have_attribute(
        "aria-current", "page"
    )
