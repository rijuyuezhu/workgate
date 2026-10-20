"""Real Chromium: Control MCP OAuth subflow UI at desktop and narrow widths."""

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120)]


def test_control_mcp_oauth_browser_actions(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    page = harness.page
    page.set_viewport_size({"width": 1360, "height": 900})
    page.goto(f"{harness.base_url}/ui#executors")
    expect(page.locator("#executor-list .executor-row").first).to_be_visible()
    page.locator("#executor-list .executor-row").first.click()
    page.locator('[data-executor-subtab="mcp"]').click()
    page.locator("#executor-mcp-add").click()
    page.locator("#executor-integration-owner").select_option("control")
    page.locator("#executor-integration-name").fill("oauth-example")
    page.locator("#executor-integration-url").fill("https://example.test/mcp")
    page.locator("#executor-integration-auth-mode").select_option("oauth")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()

    row = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="oauth-example"
    )
    expect(row).to_be_visible()
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-mcp-oauth-authorize")).to_be_visible()
    expect(page.locator("#executor-mcp-oauth-link")).to_be_hidden()
    expect(page.locator("#executor-mcp-oauth-check")).to_be_hidden()

    initiated = []
    authorization_url = "https://auth.example.test/authorize?state=sdk-csrf"

    def start(route):
        initiated.append(route.request.post_data_json)
        route.fulfill(
            json={
                "ok": True,
                "data": {
                    "session_id": "browser_session_id_000000000000000001",
                    "authorization_url": authorization_url,
                    "expires_in": 300,
                },
            }
        )

    page.route("**/api/ui/integrations/oauth/start", start)
    page.wait_for_timeout(200)
    assert not initiated  # Inventory refresh must not trigger authorization.
    page.locator("#executor-mcp-oauth-authorize").click()
    expect(page.locator("#executor-mcp-oauth-link")).to_be_visible()
    expect(page.locator("#executor-mcp-oauth-link")).to_have_attribute(
        "href", authorization_url
    )
    assert initiated == [{"owner": "control", "name": "oauth-example"}]

    # Reload without starting OAuth again: recover the same owner-bound session.
    recovered = []

    def current(route):
        recovered.append(route.request.url)
        route.fulfill(
            json={
                "ok": True,
                "data": {
                    "session_id": "browser_session_id_000000000000000001",
                    "authorization_url": authorization_url,
                    "status": "waiting",
                    "expires_in": 300,
                },
            }
        )

    page.route("**/api/ui/integrations/oauth/current?*", current)
    page.reload()
    page.locator('[data-executor-subtab="mcp"]').click()
    row = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="oauth-example"
    )
    expect(row).to_be_visible()
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-mcp-oauth-link")).to_have_attribute(
        "href", authorization_url
    )
    assert initiated == [{"owner": "control", "name": "oauth-example"}]
    assert len(recovered) == 1

    # Cancel a still-pending flow on mobile before any token exchange.
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#executor-mcp-oauth-link")).to_be_visible()
    page.route(
        "**/api/ui/integrations/oauth/cancel",
        lambda route: route.fulfill(
            json={"ok": True, "data": {"status": "cancelled"}}
        ),
    )
    page.locator("#executor-mcp-oauth-cancel").click()
    expect(page.locator("#executor-mcp-oauth-link")).to_be_hidden()
    expect(page.locator("#executor-mcp-oauth-check")).to_be_hidden()

    # Retry, then observe callback and complete in the initiating browser.
    page.locator("#executor-mcp-oauth-authorize").click()
    expect(page.locator("#executor-mcp-oauth-link")).to_be_visible()
    page.route(
        "**/api/ui/integrations/oauth/status?*",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "data": {
                    "status": "awaiting_confirmation",
                    "name": "oauth-example",
                },
            }
        ),
    )
    page.locator("#executor-mcp-oauth-check").click()
    expect(page.locator("#executor-mcp-oauth-progress")).to_have_text(
        "awaiting_confirmation"
    )
    expect(page.locator("#executor-mcp-oauth-link")).to_be_hidden()
    expect(page.locator("#executor-mcp-oauth-complete")).to_be_visible()
    completed = []

    def complete(route):
        completed.append(route.request.post_data_json)
        route.fulfill(json={"ok": True, "data": {"status": "completing"}})

    page.route("**/api/ui/integrations/oauth/complete", complete)
    page.locator("#executor-mcp-oauth-complete").click()
    expect(page.locator("#executor-mcp-oauth-progress")).to_have_text(
        "completing"
    )
    expect(page.locator("#executor-mcp-oauth-cancel")).to_be_hidden()
    assert completed == [
        {"session_id": "browser_session_id_000000000000000001"}
    ]

    page.set_viewport_size({"width": 1360, "height": 900})
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    harness.assert_clean_browser()
