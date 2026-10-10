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
        panel: getComputedStyle(document.querySelector('.dashboard-summary .dashboard-card')).backgroundColor,
        hero: getComputedStyle(document.querySelector('.dashboard-health-banner')).backgroundColor,
    })""")
    assert colors["body"] == "rgb(245, 246, 250)"
    assert colors["sidebar"] == "rgb(17, 24, 39)"
    assert colors["panel"] == "rgb(255, 255, 255)"
    assert colors["hero"] != colors["panel"]
    assert page.locator(".dashboard-summary .dashboard-card").count() == 4
    assert page.locator(".tasks-summary > div").count() == 4
    assert page.locator(".file-layout > *").count() == 3
    assert page.locator(".executor-summary > article").count() == 4
    expect(page.locator(".dashboard-health-banner")).to_have_attribute(
        "data-health", "healthy"
    )
    expect(
        page.locator(".dashboard-status-circle .dashboard-health-check")
    ).to_be_visible()
    expect(page.locator("#dashboard-health-card")).to_have_css(
        "background-color", "rgba(0, 0, 0, 0)"
    )
    expect(page.locator(".dashboard-metric-icon svg")).to_have_count(4)
    expect(page.locator(".dashboard-live").first).to_have_text("Live")
    expect(page.locator("#dashboard-network-down")).to_be_visible()
    expect(page.locator("#dashboard-network-up")).to_be_visible()
    expect(page.locator("#dashboard-cpu-foot-cores")).to_contain_text("cores")
    expect(page.locator(".token-row")).to_have_css("display", "flex")
    # Navigation icon strokes must not leak into dashboard data SVGs.
    expect(page.locator('.nav-item[data-view="overview"] svg')).to_have_css(
        "stroke-width", "1.8px"
    )
    expect(page.locator("#dashboard-cpu-trend")).to_have_css("stroke", "none")
    trend_bounds = page.locator("#dashboard-cpu-trend").bounding_box()
    assert trend_bounds and trend_bounds["width"] > 80
    # Never claim authentication or a live Control connection from static chrome.
    expect(page.locator(".status-orb, .controller-meta")).to_have_count(0)

    page.get_by_role("button", name="Manage executors →").click()
    expect(page.locator("#page-location")).to_have_text("Executors")
    harness.navigate("overview")
    page.locator("#dashboard-machine-list button").first.click()
    expect(page.locator("#page-location")).to_have_text("Executors")
    expect(page.locator(".executor-row-selected")).to_be_visible()
    harness.navigate("overview")
    page.get_by_role("button", name="Open audit activity →").click()
    expect(page.locator("#page-location")).to_have_text("Audit")
    page.locator("#audit-advanced-toggle").click()
    expect(page.locator("#audit-advanced")).to_be_visible()
    page.locator("#audit-advanced-toggle").click()
    expect(page.locator("#audit-advanced")).to_be_hidden()
    page.locator("#audit-live-toggle").click()
    expect(page.locator("#audit-live-toggle")).to_have_attribute(
        "aria-pressed", "true"
    )
    page.locator("#audit-live-toggle").click()
    expect(page.locator("#audit-live-toggle")).to_have_attribute(
        "aria-pressed", "false"
    )
    expect(page.locator("#audit-time")).to_have_value("86400")

    harness.navigate("files")
    expect(page.locator("#file-location-executors button")).to_have_count(1)
    expect(page.locator(".file-column-heading span")).to_have_count(3)
    page.keyboard.press("Control+l")
    expect(page.locator("#file-path")).to_be_focused()
    page.keyboard.press("Control+f")
    expect(page.locator("#file-filter")).to_be_focused()

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
        if view == "files":
            # A flex-basis measured as column height must not create a giant
            # empty executor selector and push the file list off-screen.
            selector_bounds = page.locator(
                ".file-executor-control"
            ).bounding_box()
            form_bounds = page.locator("#file-path-form").bounding_box()
            list_bounds = page.locator("#file-list").bounding_box()
            assert selector_bounds and selector_bounds["height"] < 90
            assert form_bounds and form_bounds["height"] < 70
            assert list_bounds and list_bounds["y"] < 780
        if view in ("overview", "executors", "files", "terminals", "tasks"):
            page.screenshot(
                path=str(harness.artifacts / f"webui-{view}-mobile.png")
            )
    # Cover narrow phones and the tablet/icon-rail breakpoints, not only 390px.
    for width in (320, 768, 1024):
        page.set_viewport_size({"width": width, "height": 800})
        for view in VIEWS:
            harness.navigate(view)
            assert page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth + 1"
            ), (width, view)
            button = page.locator(f'.nav-item[data-view="{view}"]')
            expect(button).to_be_visible()
            bounds = button.bounding_box()
            assert bounds and bounds["x"] >= 0, (width, view, bounds)
            assert bounds["x"] + bounds["width"] <= width + 1, (
                width,
                view,
                bounds,
            )
    page.set_viewport_size({"width": 1280, "height": 720})
    harness.navigate("overview")
