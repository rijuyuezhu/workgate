"""Executor details are requested on selection, not repeated on inventory polls."""

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
    expect(page.locator("#executor-diagnostics-body")).to_contain_text("Network throughput")
    assert len(calls) == 1
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    bounds = page.locator("#executor-list").bounding_box()
    assert bounds is not None and 220 <= bounds["width"] <= 260
    expect(page.locator('[data-executor-subtab="overview"]')).to_have_attribute(
        "aria-selected", "true"
    )
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_contain_text(
        "Control"
    )
    expect(page.locator("#executor-mcp-body")).to_contain_text(
        "Executor"
    )
    page.locator('[data-executor-subtab="skills"]').click()
    expect(page.locator("#executor-skills-body")).to_contain_text(
        "Executor"
    )
    page.locator('[data-executor-subtab="overview"]').click()
    expect(page.locator("#executor-diagnostics-body")).to_contain_text("Network throughput")
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Memory"
    )
    assert len(calls) == 1
    page.evaluate(
        "window.firstDiagnosticNode = document.querySelector('.executor-manage-group')"
    )
    page.wait_for_timeout(4500)
    assert page.evaluate(
        "document.querySelector('.executor-manage-group') === window.firstDiagnosticNode"
    ), "Executor inventory polling replaced diagnostic DOM"
    assert len(calls) == 1, (
        "4-second executor inventory poll must not repeat diagnostic RPC"
    )
    page.locator("#executor-diagnostics-refresh").click()
    expect(page.locator("#executor-diagnostics-body")).to_contain_text("Network throughput")
    assert len(calls) == 2
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#executor-list .executor-row").first.click()
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
