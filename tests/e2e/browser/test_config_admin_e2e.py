"""Live Chromium consent and read-only, unmasked config inspection."""

from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def test_explicit_admin_consent_then_control_and_executor_config(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    h.wait_executor_online()
    page = h.page
    h.navigate("executors")
    page.locator("#executor-control-config-open").click()
    body = page.locator("#executor-control-config-body")
    expect(body).to_contain_text("config:admin OAuth grant")
    assert h.admin_pin not in body.inner_text()
    body.get_by_role("button", name="Authorize configuration access").click()
    page.wait_for_url("**/oauth/authorize?*")
    assert (
        "config:admin" in parse_qs(urlparse(page.url).query)["scope"][0].split()
    )
    page.locator('input[name="pin"]').fill(h.admin_pin)
    page.get_by_role("button", name="Approve").click()
    page.wait_for_url("**/ui/callback*")
    expect(page.locator("#connection-state")).to_have_text("Connected")

    h.navigate("executors")
    page.locator("#executor-control-config-open").click()
    body = page.locator("#executor-control-config-body")
    expect(body).to_contain_text("oauth_admin_pin")
    expect(body).to_contain_text(h.admin_pin)
    expect(body).to_contain_text("Effective")
    # Open a nullable setting's group: explicit null is not an absent file field.
    expect(body.locator("details").first).to_be_visible()
    expect(page.locator("#executors-panel .executors-layout")).to_be_hidden()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )
    expect(
        body.locator(".executor-config-table tbody tr").first
    ).to_be_visible()
    page.locator("#executor-control-config-close").click()
    expect(page.locator("#executors-panel .executors-layout")).to_be_visible()
    page.set_viewport_size({"width": 1360, "height": 900})
    assert h.admin_pin not in body.inner_text()
    page.locator("#executor-control-config-open").click()
    expect(body).to_contain_text(h.admin_pin)
    h.navigate("overview")
    assert h.admin_pin not in body.inner_text()
    h.navigate("executors")

    page.locator("#executor-list button").first.click()
    page.locator('#executor-subtabs [data-executor-subtab="config"]').click()
    page.locator("#executor-config-refresh").click()
    executor = page.locator("#executor-config-body")
    expect(executor).to_be_visible()
    expect(executor).to_contain_text("max_jobs")
    expect(executor).to_contain_text("Source")
    assert executor.evaluate("el => !!el.closest('.executor-details')")
    page.locator('#executor-subtabs [data-executor-subtab="overview"]').click()
    assert not executor.inner_text()

    # The initial, expected HTTP 403 browser-resource error is not a script
    # failure. All other console/network exceptions remain disallowed.
    assert all("403" in message for message in h.console_errors)
    h.console_errors.clear()
    h.assert_clean_browser()
