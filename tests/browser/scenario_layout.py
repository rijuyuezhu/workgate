"""Upstream v5 WebUI shell and responsive page-structure regression."""

from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness

VIEWS = ("overview", "tasks", "files", "terminals", "executors", "audit")


def run_webui_layout(harness: BrowserHarness) -> None:
    page = harness.page
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.locator(".sidebar")).to_be_visible()
    expect(page.locator(".topbar")).to_be_visible()
    assert page.title().endswith("· Workgate")
    colors = page.evaluate("""() => ({
        body: getComputedStyle(document.body).backgroundColor,
        sidebar: getComputedStyle(document.querySelector('.sidebar')).backgroundColor,
        panel: getComputedStyle(document.querySelector('#dashboard-panel')).backgroundColor,
    })""")
    assert colors["body"] == "rgb(245, 246, 250)"
    assert colors["sidebar"] == "rgb(17, 24, 39)"
    assert colors["panel"] == "rgb(255, 255, 255)"
    expect(page.locator(".token-row")).to_have_css("display", "flex")

    for view in VIEWS:
        harness.navigate(view)
        expect(page.locator("#page-location")).to_have_text(view.capitalize())
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth + 1"
        )
        if view in ("overview", "executors", "files", "terminals", "tasks"):
            page.screenshot(
                path=str(harness.artifacts / f"webui-{view}-desktop.png")
            )

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("""() => {
        const box = document.querySelector('.sidebar').getBoundingClientRect();
        return box.bottom <= innerHeight + 1 && box.bottom >= innerHeight - 1;
    }""")
    for view in VIEWS:
        harness.navigate(view)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth + 1"
        ), view
        active = page.locator(f'.nav-item[data-view="{view}"]')
        expect(active).to_be_visible()
        expect(active).to_have_attribute("aria-current", "page")
        if view in ("overview", "executors", "files", "terminals", "tasks"):
            page.screenshot(
                path=str(harness.artifacts / f"webui-{view}-mobile.png")
            )
    page.set_viewport_size({"width": 1280, "height": 720})
    harness.navigate("overview")
