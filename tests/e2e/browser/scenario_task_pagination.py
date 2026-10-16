"""Task list paging, status filters, pinned selections and legacy discoverability."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def run_task_pagination_ui(harness: BrowserHarness) -> None:
    page = harness.page
    page.set_viewport_size({"width": 1360, "height": 900})
    fresh = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "pagination-selected"},
    )
    assert fresh["status"] == 200
    selected = fresh["payload"]["structuredContent"]["result"]["task_id"]
    extra = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "pagination-extra"},
    )
    assert extra["status"] == 200
    legacy = harness.api(
        "POST",
        "/tools/session_start",
        body={
            "workdir": ".",
            "label": "pagination-legacy",
            "executor_id": harness.executor_id,
        },
    )
    assert legacy["status"] == 200 and not legacy["payload"].get("isError"), (
        legacy
    )
    legacy_data = harness.api("GET", "/api/ui/tasks?status=unattached&limit=10")
    assert legacy_data["status"] == 200, legacy_data
    assert legacy_data["payload"]["data"]["total"] >= 1, (legacy, legacy_data)

    # Shrink only this browser scenario's server page to exercise Next/Previous
    # against real server cursors, rather than creating 51+ expensive fixtures.
    def small_pages(route):
        url = urlsplit(route.request.url)
        query = dict(parse_qsl(url.query))
        query["limit"] = "1"
        requested = urlunsplit(url._replace(query=urlencode(query)))
        route.fulfill(response=route.fetch(url=requested))

    pattern = "**/api/ui/tasks?**"
    page.route(pattern, small_pages)
    try:
        page.goto(f"{harness.base_url}/ui?task_id={selected}#tasks")
        expect(page.locator("#connection-state")).to_have_text("Connected")
        expect(page.locator("#tasks-filter")).to_have_value("active")
        expect(page.locator("#task-list .task-entry")).to_have_count(2)
        expect(
            page.locator(f'#task-list [data-task-id="{selected}"]')
        ).to_have_attribute("aria-current", "true")
        expect(page.locator("#tasks-next")).to_be_enabled()
        page.locator("#tasks-next").click()
        expect(page.locator("#tasks-page-state")).to_contain_text("Page 2")
        expect(page.locator("#tasks-prev")).to_be_enabled()
        page.locator("#tasks-prev").click()
        expect(page.locator("#tasks-page-state")).to_contain_text("Page 1")
        page.locator("#tasks-filter").select_option("completed")
        expect(
            page.locator(f'#task-list [data-task-id="{selected}"]')
        ).to_have_count(1)
        expect(page.locator("#task-status")).to_have_text("active")
        page.locator("#tasks-filter").select_option("unattached")
        expect(
            page.locator("#task-list .unattached-session-entry").first
        ).to_be_visible()
        expect(
            page.locator(f'#task-list [data-task-id="{selected}"]')
        ).to_have_count(1)
        page.locator("#tasks-filter").select_option("active")
        expect(page.locator("#tasks-prev")).to_be_disabled()

        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator(".task-list-controls")).to_be_hidden()
        page.locator(".mobile-subpage-bar:visible .mobile-subpage-back").click()
        expect(page.locator("#tasks-filter")).to_be_visible()
        expect(page.locator("#tasks-next")).to_be_enabled()
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth + 1"
        )
        page.screenshot(
            path=str(harness.artifacts / "mobile-task-filter-page.png")
        )
    finally:
        page.unroute(pattern, small_pages)
        page.set_viewport_size({"width": 1360, "height": 900})
