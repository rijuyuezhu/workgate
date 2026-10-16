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
    # An inventory poll alone must never start a Skill scan or MCP probe.
    assert not calls
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    expect(page.locator("#executor-diagnostics-state")).to_contain_text(
        "On-demand snapshot"
    )
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Control-owned network MCP"
    )
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Executor-local stdio MCP"
    )
    expect(page.locator("#executor-diagnostics-body")).to_contain_text(
        "Executor Skill sources"
    )
    assert len(calls) == 1
    page.evaluate(
        "window.firstDiagnosticNode = document.querySelector('.executor-diagnostics-group')"
    )
    page.wait_for_timeout(4500)
    assert page.evaluate(
        "document.querySelector('.executor-diagnostics-group') === window.firstDiagnosticNode"
    ), "Executor inventory polling replaced diagnostic DOM"
    assert len(calls) == 1, (
        "4-second executor inventory poll must not repeat diagnostic RPC"
    )
    page.locator("#executor-diagnostics-refresh").click()
    expect(page.locator("#executor-diagnostics-state")).to_contain_text(
        "On-demand snapshot"
    )
    assert len(calls) == 2
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#executor-list .executor-row").first.click()
    expect(page.locator("#executor-diagnostics-body")).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(
        path=str(harness.artifacts / "executor-diagnostics-mobile.png")
    )
