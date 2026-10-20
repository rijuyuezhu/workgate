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


def test_enabled_mcp_rows_distinguish_private_auth_without_probes(
    browser_harness: BrowserHarness,
) -> None:
    """Only enabled MCPs that need credentials show authorization readiness."""
    from urllib.parse import parse_qs, urlsplit

    harness = browser_harness
    harness.login()
    page = harness.page
    unauthorized_calls = []

    def management(route):
        params = parse_qs(urlsplit(route.request.url).query)
        if params.get("owner") != ["control"] or params.get("kind") != ["mcp"]:
            route.continue_()
            return
        rows = [
            ("normal-mcp", "none", True, None),
            ("oauth-pending", "oauth", True, False),
            ("secret-ready", "secret", True, True),
            ("oauth-disabled", "oauth", False, False),
        ]
        route.fulfill(
            json={
                "ok": True,
                "data": {
                    "can_manage": True,
                    "servers": [
                        {
                            "name": name,
                            "type": "http",
                            "enabled": enabled,
                            "auth": {"mode": mode},
                            **(
                                {"auth_authorized": authorized}
                                if authorized is not None
                                else {}
                            ),
                        }
                        for name, mode, enabled, authorized in rows
                    ],
                },
            }
        )

    page.route("**/api/ui/integrations?*", management)
    page.on(
        "request",
        lambda req: (
            unauthorized_calls.append(req.url)
            if "/api/ui/integrations/test" in req.url
            or "/api/ui/integrations/oauth/start" in req.url
            else None
        ),
    )
    page.goto(f"{harness.base_url}/ui#executors")
    page.locator('[data-executor-subtab="mcp"]').click()
    entries = page.locator("#executor-mcp-body .executor-manage-row")
    normal = entries.filter(has_text="normal-mcp")
    pending = entries.filter(has_text="oauth-pending")
    ready = entries.filter(has_text="secret-ready")
    disabled = entries.filter(has_text="oauth-disabled")
    for entry in [normal, pending, ready, disabled]:
        expect(entry).to_be_visible()

    expect(normal).to_have_attribute("data-state", "enabled")
    assert normal.get_attribute("data-auth") is None
    assert "auth" not in normal.inner_text().lower().replace("normal-mcp", "")
    expect(pending).to_have_attribute("data-auth", "required")
    expect(pending.locator(".executor-row-auth")).to_have_text(
        "Authorization required"
    )
    expect(ready).to_have_attribute("data-auth", "authorized")
    expect(ready.locator(".executor-row-auth")).to_have_text(
        "✓ Secret configured"
    )
    expect(disabled).to_have_attribute("data-state", "disabled")
    assert disabled.get_attribute("data-auth") is None

    # Color differs visibly from the ordinary (light-green) enabled row.
    backgrounds = [
        entry.evaluate("(element) => getComputedStyle(element).backgroundColor")
        for entry in [normal, pending, ready, disabled]
    ]
    assert backgrounds[0] != backgrounds[1]
    assert backgrounds[1] != backgrounds[2]
    assert not unauthorized_calls
    page.set_viewport_size({"width": 390, "height": 844})
    # Mobile uses a list → detail drilldown, not the desktop split pane.
    page.locator("#executor-list .executor-row").first.click()
    page.locator('[data-executor-subtab="mcp"]').click()
    for entry in [normal, pending, ready, disabled]:
        expect(entry).to_be_visible()
        assert entry.evaluate(
            "(element) => element.scrollWidth <= element.clientWidth + 2"
        )
    harness.assert_clean_browser()
