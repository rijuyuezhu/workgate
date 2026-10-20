"""Real Chromium: minimal MCP OAuth UI, link copy and passive completion."""

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
    expect(page.locator("#executor-mcp-secret-details")).to_be_hidden()
    expect(page.locator("#executor-integration-test")).to_be_hidden()
    expect(page.locator("#executor-mcp-oauth-link")).to_be_hidden()

    initiated = []
    authorization_url = "https://auth.example.test/authorize?state=sdk-csrf"
    sid = "browser_session_id_000000000000000001"

    def start(route):
        initiated.append(route.request.post_data_json)
        route.fulfill(
            json={
                "ok": True,
                "data": {
                    "session_id": sid,
                    "authorization_url": authorization_url,
                    "expires_in": 300,
                },
            }
        )

    page.route("**/api/ui/integrations/oauth/start", start)
    page.route(
        "**/api/ui/integrations/oauth/status?*",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "data": {"status": "waiting", "name": "oauth-example"},
            }
        ),
    )
    page.wait_for_timeout(200)
    assert not initiated  # Opening inventory must not start third-party OAuth.
    page.locator("#executor-mcp-oauth-authorize").click()
    expect(page.locator("#executor-mcp-oauth-link")).to_have_attribute(
        "href", authorization_url
    )
    expect(page.locator("#executor-mcp-oauth-copy")).to_be_visible()
    assert initiated == [{"owner": "control", "name": "oauth-example"}]
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.locator("#executor-mcp-oauth-copy").click()
    expect(page.locator("#executor-mcp-oauth-progress")).to_contain_text(
        "Link copied"
    )
    assert page.evaluate("navigator.clipboard.readText()") == authorization_url

    # Reload: recover the same owner's link without another OAuth start.
    recovered = []

    def current(route):
        recovered.append(route.request.url)
        route.fulfill(
            json={
                "ok": True,
                "data": {
                    "session_id": sid,
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
    assert len(recovered) == 1
    assert len(initiated) == 1

    # Mobile: pending OAuth can be cancelled without a manual Complete step.
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#executor-mcp-oauth-copy")).to_be_visible()
    page.route(
        "**/api/ui/integrations/oauth/cancel",
        lambda route: route.fulfill(
            json={"ok": True, "data": {"status": "cancelled"}}
        ),
    )
    page.locator("#executor-mcp-oauth-cancel").click()
    expect(page.locator("#executor-mcp-oauth-link")).to_be_hidden()
    expect(page.locator("#executor-mcp-oauth-authorize")).to_be_visible()

    # Restart; passive polling receives an automatic backend completion.
    polls = []

    def status(route):
        polls.append(route.request.url)
        state = "completing" if len(polls) == 1 else "authorized"
        route.fulfill(
            json={
                "ok": True,
                "data": {"status": state, "name": "oauth-example"},
            }
        )

    page.route("**/api/ui/integrations/oauth/status?*", status)
    page.route(
        "**/api/ui/integrations/auth?*",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "data": {
                    "name": "oauth-example",
                    "auth": {
                        "mode": "oauth",
                        "authorized": True,
                        "status": "authorized",
                    },
                    "secret_names": [],
                },
            }
        ),
    )
    page.route(
        "**/api/ui/integrations/oauth/current?*",
        lambda route: route.fulfill(json={"ok": True, "data": None}),
    )
    page.locator("#executor-mcp-oauth-authorize").click()
    expect(page.locator("#executor-mcp-auth-status")).to_contain_text(
        "Connected", timeout=16000
    )
    expect(page.locator("#executor-mcp-oauth-logout")).to_be_visible()
    expect(page.locator("#executor-mcp-oauth-link")).to_be_hidden()
    assert len(polls) >= 2
    assert len(initiated) == 2
    assert not page.locator("#executor-mcp-oauth-complete").count()

    page.set_viewport_size({"width": 1360, "height": 900})
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    harness.assert_clean_browser()
