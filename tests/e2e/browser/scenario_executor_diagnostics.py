"""Executor details are requested on selection, not repeated on inventory polls."""

import re

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def run_executor_diagnostics_ui(harness: BrowserHarness) -> None:
    page = harness.page
    calls: list[str] = []
    page.on(
        "request",
        lambda request: (
            calls.append(request.url)
            if "/api/ui/executors/diagnostics?" in request.url
            else None
        ),
    )
    page.set_viewport_size({"width": 1360, "height": 900})
    page.goto(f"{harness.base_url}/ui#executors")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#executor-list .executor-row")).to_be_visible()
    # Initial auto-selection loads exactly one snapshot; polling must not repeat it.
    expect(page.locator("#executor-overview-metrics")).to_contain_text(
        "Network throughput"
    )
    assert len(calls) >= 1
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    bounds = page.locator("#executor-list").bounding_box()
    assert bounds is not None and 220 <= bounds["width"] <= 260
    expect(page.locator('[data-executor-subtab="overview"]')).to_have_attribute(
        "aria-selected", "true"
    )
    # ARIA tabs must also be operable with the keyboard, not only clicks.
    page.locator('[data-executor-subtab="overview"]').focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator('[data-executor-subtab="mcp"]')).to_have_attribute(
        "aria-selected", "true"
    )
    page.keyboard.press("End")
    expect(
        page.locator('[data-executor-subtab="diagnostics"]')
    ).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Home")
    expect(page.locator('[data-executor-subtab="overview"]')).to_have_attribute(
        "aria-selected", "true"
    )
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_contain_text("Control")
    expect(page.locator("#executor-mcp-body")).to_contain_text("Executor")
    expect(page.locator("#executor-mcp-body")).not_to_contain_text("Loading…")
    page.locator('[data-executor-subtab="skills"]').click()
    expect(page.locator("#executor-skills-body")).to_contain_text("Executor")
    page.locator('[data-executor-subtab="diagnostics"]').click()
    expect(page.locator("#executor-diagnostics-state")).to_contain_text(
        "On-demand snapshot"
    )
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Agent Bridge"
    )
    page.locator('[data-executor-subtab="overview"]').click()
    expect(page.locator("#executor-overview-metrics")).to_contain_text(
        "Network throughput"
    )
    expect(page.locator("#executor-overview-metrics")).to_contain_text("Memory")
    initial_calls = len(calls)
    page.evaluate(
        "window.firstDiagnosticNode = document.querySelector('.executor-manage-group')"
    )
    page.wait_for_timeout(4500)
    assert page.evaluate(
        "document.querySelector('.executor-manage-group') === window.firstDiagnosticNode"
    ), "Executor inventory polling replaced diagnostic DOM"
    assert len(calls) == initial_calls, (
        "4-second executor inventory poll must not repeat diagnostic RPC"
    )
    expect(page.locator("#executor-overview-metrics")).to_contain_text(
        "Network throughput"
    )
    assert (
        len(calls) == initial_calls
    )  # Resource refresh uses Dashboard telemetry.
    expect(
        page.locator(
            "#executor-overview-metrics .dashboard-sparkline polyline"
        ).first
    ).to_be_attached(timeout=12000)

    # A peer may omit allowlisted fields. One malformed MCP entry must not
    # interrupt the rest of the Executor details or hide configured Control MCPs.
    def incomplete_diagnostics(route):
        response = route.fetch()
        payload = response.json()
        local = payload["data"]["executor"]
        local["bridge"]["status"] = "loaded"
        local["bridge"]["servers"] = [
            {"name": "type-omitted", "enabled": True},
            {"name": "local-enabled", "type": "stdio", "enabled": True},
            {"name": "local-disabled", "type": "stdio", "enabled": False},
        ]
        local["skills"]["selected"] = [
            {"name": "read-only-global", "source": "global"}
        ]
        local["skills"]["overridden"] = [
            {"name": "shadowed-global", "source": "global"}
        ]
        payload["data"]["control_mcp"] = {
            "status": "loaded",
            "enabled": False,
            "servers": [
                {"name": "control-configured", "type": "http", "enabled": True}
            ],
            "errors": 0,
        }
        route.fulfill(response=response, json=payload)

    diagnostic_url = re.compile(r"/api/ui/executors/diagnostics(?:\?|$)")
    intercepted: list[str] = []

    def capture_diagnostics(route):
        intercepted.append(route.request.url)
        incomplete_diagnostics(route)

    page.route(diagnostic_url, capture_diagnostics)
    page.reload()
    expect(
        page.locator("#executor-list .executor-row")
        .filter(has_text="online")
        .first
    ).to_be_visible()
    page.locator('[data-executor-subtab="diagnostics"]').click()
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Agent Bridge"
    )
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_contain_text("Control")
    page.locator('[data-executor-subtab="skills"]').click()
    expect(page.locator("#executor-skills-body")).to_contain_text(
        "read-only-global"
    )
    expect(page.locator("#executor-skills-body")).to_contain_text(
        "shadowed-global"
    )
    expect(
        page.locator("#executor-skills-body .executor-manage-row").filter(
            has_text="shadowed-global"
        )
    ).to_contain_text("Overridden")
    assert intercepted, "malformed MCP response interception did not run"
    page.unroute(diagnostic_url, capture_diagnostics)
    page.locator('[data-executor-subtab="overview"]').click()

    def offline_inventory(route):
        response = route.fetch()
        payload = response.json()
        for entry in payload.get("data", {}).get("executors", []):
            if entry.get("executor_id") == harness.executor_id:
                entry["online"] = False
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/executors", offline_inventory)
    expect(page.locator("#executor-detail-status")).to_have_text(
        "offline", timeout=12000
    )
    expect(page.locator("#executor-overview-metrics")).to_be_hidden()
    expect(page.locator("#executor-detail-id")).not_to_have_text("—")
    # Inventory can mark a peer offline before the cached diagnostics expire.
    # Do not show its previous MCP / Skills as if they were currently available.
    page.locator('[data-executor-subtab="skills"]').click()
    expect(page.locator("#executor-skills-body")).to_contain_text("offline")
    expect(page.locator("#executor-skills-body")).not_to_contain_text(
        "read-only-global"
    )
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_contain_text("offline")
    expect(page.locator("#executor-mcp-body")).to_contain_text("Control")
    page.unroute("**/api/ui/executors", offline_inventory)
    expect(page.locator("#executor-detail-status")).to_have_text(
        "online", timeout=12000
    )
    page.locator('[data-executor-subtab="overview"]').click()
    expect(page.locator("#executor-overview-metrics")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    # Mobile starts at the Executor list; enter the details subpage first.
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="overview"]').click()
    expect(page.locator("#executor-overview-metrics")).to_be_visible()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(
        path=str(harness.artifacts / "executor-diagnostics-mobile.png")
    )
