"""Upstream v5 WebUI shell and responsive page-structure regression."""

from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness

VIEWS = ("overview", "tasks", "files", "terminals", "executors", "audit")


def run_webui_layout(harness: BrowserHarness) -> None:
    page = harness.page
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.locator(".app-header")).to_be_visible()
    expect(page.locator(".brand-block")).to_have_attribute(
        "data-go-view", "overview"
    )
    expect(page.locator("#nav-toggle")).to_be_hidden()
    expect(page.locator("#connection-state")).to_be_visible()
    expect(page.locator("#version")).to_be_visible()
    assert page.title().endswith("· Workgate")
    colors = page.evaluate("""() => ({
        body: getComputedStyle(document.body).backgroundColor,
        header: getComputedStyle(document.querySelector('.app-header')).backgroundColor,
        panel: getComputedStyle(document.querySelector('.dashboard-summary .dashboard-card')).backgroundColor,
        hero: getComputedStyle(document.querySelector('.dashboard-health-banner')).backgroundColor,
    })""")
    assert colors["body"] == "rgb(245, 246, 250)"
    assert colors["header"] == "rgb(17, 24, 39)"
    header_bounds = page.locator(".app-header").bounding_box()
    content_bounds = page.locator(".main-content").bounding_box()
    assert header_bounds and content_bounds
    assert (
        content_bounds["y"] >= header_bounds["y"] + header_bounds["height"] - 1
    )
    nav_boxes = [
        box
        for item in page.locator(".nav-item").all()
        if (box := item.bounding_box()) is not None
    ]
    assert len(nav_boxes) == 6
    assert (
        max(box["y"] for box in nav_boxes) - min(box["y"] for box in nav_boxes)
        < 2
    )
    assert all(
        nav_boxes[i]["x"] + nav_boxes[i]["width"] + 5 < nav_boxes[i + 1]["x"]
        for i in range(5)
    )
    assert colors["panel"] == "rgb(255, 255, 255)"
    assert colors["hero"] != colors["panel"]
    assert page.locator(".dashboard-summary .dashboard-card").count() == 4
    expect(page.locator(".dashboard-activity-section")).to_have_count(0)
    assert (
        page.locator(".dashboard-executors-row > .dashboard-section").count()
        == 3
    )
    sections = [
        item.bounding_box()
        for item in page.locator(
            ".dashboard-executors-row > .dashboard-section"
        ).all()
    ]
    section_tops = [section["y"] for section in sections if section is not None]
    assert len(section_tops) == 3
    assert max(section_tops) - min(section_tops) < 2
    assert page.locator(".inventory-summary").count() == 2
    assert page.locator(".file-layout > *").count() == 3
    assert page.locator(".tasks-summary, .executor-summary").count() == 0
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
    expect(page.locator("#page-title")).to_have_text("Executors")
    harness.navigate("overview")
    page.get_by_role("button", name="Open Audit records").click()
    expect(page.locator("#page-title")).to_have_text("Audit")
    harness.navigate("overview")
    page.get_by_role("button", name="View audit →").click()
    expect(page.locator("#page-title")).to_have_text("Audit")
    harness.navigate("overview")
    page.locator("#dashboard-machine-list button").first.click()
    expect(page.locator("#page-title")).to_have_text("Overview")
    expect(page.locator(".dashboard-executor-control")).to_be_hidden()
    expect(
        page.locator("#dashboard-machine-list button").first
    ).to_have_attribute("aria-current", "true")
    harness.navigate("overview")
    page.get_by_role("button", name="Open Audit records").click()
    expect(page.locator("#page-title")).to_have_text("Audit")
    audit_height = page.locator(".audit-panel > .audit-layout").evaluate(
        "element => element.getBoundingClientRect().height"
    )
    page_height = page.evaluate("document.documentElement.scrollHeight")
    page.locator("#audit-advanced-toggle").click()
    expect(page.locator("#audit-advanced")).to_be_visible()
    assert (
        page.locator(".audit-panel > .audit-layout").evaluate(
            "element => element.getBoundingClientRect().height"
        )
        == audit_height
    )
    assert page.evaluate("document.documentElement.scrollHeight") == page_height
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
    expect(page.locator(".file-executor-control")).to_be_hidden()
    expect(
        page.locator("#file-location-executors button").first
    ).to_have_attribute("aria-current", "true")
    expect(page.locator(".file-column-heading span")).to_have_count(3)
    page.keyboard.press("Control+l")
    expect(page.locator("#file-path")).to_be_focused()
    page.keyboard.press("Control+f")
    expect(page.locator("#file-filter")).to_be_focused()
    harness.navigate("terminals")
    expect(page.locator(".executor-select")).to_be_hidden()
    expect(page.locator("#terminal-create > .control-icon")).to_be_visible()
    expect(page.locator("#terminal-reconnect > .control-icon")).to_have_count(1)
    assert (
        page.locator(".terminal-heading").evaluate(
            "node => getComputedStyle(node).backgroundColor"
        )
        == "rgb(255, 255, 255)"
    )
    assert (
        page.locator(".terminal-rail").evaluate(
            "node => getComputedStyle(node).backgroundColor"
        )
        == "rgb(250, 251, 254)"
    )
    assert (
        page.locator("#terminal-output").evaluate(
            "node => getComputedStyle(node).backgroundColor"
        )
        == "rgb(7, 17, 31)"
    )
    assert "JetBrains Mono" in page.locator("#terminal-output").evaluate(
        "node => getComputedStyle(node).fontFamily"
    )
    expect(page.locator("#terminal-executor-locations button")).to_have_count(1)
    expect(
        page.locator("#terminal-executor-locations button").first
    ).to_have_attribute("aria-current", "true")
    assert (
        page.evaluate(
            "getComputedStyle(document.querySelector('.dashboard-health-banner')).borderRadius"
        )
        == "6px"
    )

    for view in VIEWS:
        harness.navigate(view)
        expect(page.locator("#page-title")).to_have_text(view.capitalize())
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth + 1"
        )
        if view in ("overview", "executors", "files", "terminals", "tasks"):
            page.screenshot(
                path=str(harness.artifacts / f"webui-{view}-desktop.png")
            )

    page.locator(".brand-block").click()
    expect(page.locator("#page-title")).to_have_text("Overview")
    expect(page.locator('.nav-item[data-view="overview"]')).to_have_attribute(
        "aria-current", "page"
    )

    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#nav-toggle")).to_be_visible()
    expect(page.locator("#nav-toggle")).to_have_attribute(
        "aria-expanded", "false"
    )
    expect(page.locator(".primary-nav")).to_be_hidden()
    main_box = page.locator(".main-content").bounding_box()
    assert main_box is not None
    main_top = main_box["y"]
    page.locator("#nav-toggle").click()
    expect(page.locator("#nav-toggle")).to_have_attribute(
        "aria-expanded", "true"
    )
    expect(page.locator(".primary-nav")).to_be_visible()
    mobile_boxes = [
        box
        for item in page.locator(".nav-item").all()
        if (box := item.bounding_box()) is not None
    ]
    assert len(mobile_boxes) == 6
    assert mobile_boxes[0]["x"] == mobile_boxes[2]["x"]
    assert mobile_boxes[0]["x"] < mobile_boxes[1]["x"]
    main_box = page.locator(".main-content").bounding_box()
    assert main_box is not None and main_box["y"] == main_top
    page.mouse.click(5, 610)
    expect(page.locator(".primary-nav")).to_be_hidden()
    page.locator("#nav-toggle").click()
    page.keyboard.press("Escape")
    expect(page.locator(".primary-nav")).to_be_hidden()
    expect(page.locator("#nav-toggle")).to_be_focused()
    for view in VIEWS:
        harness.navigate(view)
        expect(page.locator("#nav-toggle")).to_have_attribute(
            "aria-expanded", "false"
        )
        expect(page.locator(".primary-nav")).to_be_hidden()
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth + 1"
        ), view
        active = page.locator(f'.nav-item[data-view="{view}"]')
        expect(active).to_have_attribute("aria-current", "page")
        if view == "overview":
            expect(page.locator(".dashboard-activity-section")).to_have_count(0)
            # The resource charts and captions belong to their cards; none
            # may overlap even in two-column phone layouts.
            assert page.evaluate("""() => {
                const bounds = (node) => node.getBoundingClientRect();
                for (const card of document.querySelectorAll('.dashboard-metric-card')) {
                    const footer = card.querySelector('.dashboard-metric-foot');
                    const chart = card.querySelector('.dashboard-sparkline:not(.visually-hidden)');
                    if (chart && bounds(footer).top < bounds(chart).bottom - 1) return false;
                    const values = card.querySelector('.dashboard-network-values');
                    const bar = card.querySelector('.dashboard-network-bar');
                    if (values && bounds(bar).top < bounds(values).bottom - 1) return false;
                }
                return true;
            }""")
            assert (
                page.locator(".dashboard-health-stats").evaluate(
                    "element => getComputedStyle(element).display"
                )
                == "grid"
            )
        if view == "files":
            # Mobile has no Locations rail, so it keeps the executor selector.
            expect(page.locator(".file-executor-control")).to_be_visible()
            label_bounds = page.locator(
                ".file-executor-control > span"
            ).bounding_box()
            select_bounds = page.locator("#file-executor").bounding_box()
            assert label_bounds and select_bounds
            assert (
                select_bounds["y"]
                >= label_bounds["y"] + label_bounds["height"] + 4
            )
            assert abs(select_bounds["x"] - label_bounds["x"]) < 1
            assert abs(select_bounds["width"] - label_bounds["width"]) < 1
            form_bounds = page.locator("#file-path-form").bounding_box()
            list_bounds = page.locator("#file-list").bounding_box()
            assert form_bounds and form_bounds["height"] < 70
            assert list_bounds and list_bounds["y"] < 780
        if view in ("tasks", "executors", "terminals", "audit"):
            selectors = {
                "tasks": (".tasks-layout > .session-list", ".tasks-workspace"),
                "executors": (
                    ".executors-layout > .executor-list",
                    ".executors-layout > .executor-details",
                ),
                "terminals": (
                    ".terminal-layout > .terminal-rail",
                    ".terminal-layout > .terminal-console",
                ),
                "audit": (
                    ".audit-panel > .audit-layout > .audit-list-pane",
                    ".audit-panel > .audit-layout > .audit-detail",
                ),
            }
            upper, lower = selectors[view]
            expect(page.locator(upper)).to_be_visible()
            expect(page.locator(lower)).to_be_hidden()
            expect(page.locator(f'[data-app-view="{view}"]')).to_have_attribute(
                "data-mobile-step", "list"
            )
        if view == "files":
            expect(page.locator(".file-browser")).to_be_visible()
            expect(page.locator(".file-preview")).to_be_hidden()
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
            if page.locator("#nav-toggle").is_visible():
                page.locator("#nav-toggle").click()
            expect(button).to_be_visible()
            bounds = button.bounding_box()
            assert bounds and bounds["x"] >= 0, (width, view, bounds)
            assert bounds["x"] + bounds["width"] <= width + 1, (
                width,
                view,
                bounds,
            )
            if page.locator("#nav-toggle").is_visible():
                page.locator("#nav-toggle").click()
    page.set_viewport_size({"width": 1280, "height": 720})
    harness.navigate("overview")
