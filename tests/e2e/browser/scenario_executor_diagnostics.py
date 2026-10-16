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
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Network throughput"
    )
    page.wait_for_function(
        """() => {
      const body = document.querySelector('#executor-mcp-body');
      return body && body.querySelectorAll('.executor-manage-group').length === 2
        && !body.textContent.includes('Loading');
    }""",
        timeout=12000,
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
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_contain_text("Control")
    expect(page.locator("#executor-mcp-body")).to_contain_text("Executor")
    assert page.locator(
        "#executor-mcp-body .executor-manage-group h4"
    ).all_text_contents()[:2] == ["Executor", "Control"]
    page.locator('[data-executor-subtab="skills"]').click()
    assert page.locator(
        "#executor-skills-body .executor-manage-group h4"
    ).all_text_contents()[:2] == ["Executor", "Executor (User Global)"]
    expect(page.locator("#executor-skills-body")).to_contain_text("Executor")
    page.locator('[data-executor-subtab="overview"]').click()
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Network throughput"
    )
    expect(page.locator("#executor-diagnostics-body")).to_contain_text("Memory")
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
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Network throughput"
    )
    assert (
        len(calls) == initial_calls
    )  # Resource refresh uses Dashboard telemetry.
    expect(
        page.locator(
            "#executor-diagnostics-body .dashboard-sparkline polyline"
        ).first
    ).to_be_attached(timeout=12000)

    # A peer may omit allowlisted fields. One malformed MCP entry must not
    # interrupt the rest of the Executor details or hide configured Control MCPs.
    def incomplete_diagnostics(route):
        response = route.fetch()
        payload = response.json()
        local = payload["data"]["executor"]
        local["bridge"]["status"] = "loaded"
        local["bridge"]["servers"] = [{"name": "type-omitted", "enabled": True}]
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
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_contain_text("type-omitted")
    expect(page.locator("#executor-mcp-body")).to_contain_text("Unknown")
    expect(
        page.locator("#executor-mcp-body .executor-manage-row").filter(
            has_text="control-configured"
        )
    ).to_contain_text("Disabled")
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
    expect(page.locator("#executor-diagnostics-body")).to_be_hidden()
    expect(page.locator("#executor-detail-id")).not_to_have_text("—")
    page.unroute("**/api/ui/executors", offline_inventory)
    expect(page.locator("#executor-detail-status")).to_have_text(
        "online", timeout=12000
    )
    expect(page.locator("#executor-diagnostics-body")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    # Mobile starts at the Executor list; enter the details subpage first.
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="overview"]').click()
    expect(page.locator("#executor-diagnostics-body")).to_be_visible()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(
        path=str(harness.artifacts / "executor-diagnostics-mobile.png")
    )
