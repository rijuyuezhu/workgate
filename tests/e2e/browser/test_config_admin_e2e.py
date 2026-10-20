"""Live Chromium normal-login and automatic read-only config inspection."""

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def test_normal_login_opens_control_and_executor_config(
    browser_harness: BrowserHarness,
):
    h = browser_harness
    h.login()
    h.wait_executor_online()
    page = h.page
    h.navigate("executors")
    page.locator('[data-executor-subtab="config"]').click()
    body = page.locator("#executor-config-body")
    expect(body).to_contain_text("oauth_admin_pin")
    expect(body).to_contain_text(h.admin_pin)
    expect(body.locator(".executor-config-name").first).to_be_visible()
    # Open a nullable setting's group: explicit null is not an absent file field.
    expect(body.locator(".executor-config-section").first).to_be_visible()
    expect(page.locator("#executors-panel .executors-layout")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('#executor-list button[data-selection="__control__"]').click()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )
    expect(body.locator(".executor-config-row").first).to_be_visible()
    page.locator('[data-executor-subtab="overview"]').click()
    expect(page.locator("#executors-panel .executors-layout")).to_be_visible()
    page.set_viewport_size({"width": 1360, "height": 900})
    assert h.admin_pin not in body.inner_text()
    page.locator('[data-executor-subtab="config"]').click()
    expect(body).to_contain_text(h.admin_pin)
    h.navigate("overview")
    assert h.admin_pin not in body.inner_text()
    h.navigate("executors")

    page.locator(
        '#executor-list button:not([data-selection="__control__"])'
    ).first.click()

    page.locator('#executor-subtabs [data-executor-subtab="config"]').click()
    executor = page.locator("#executor-config-body")
    expect(executor).to_be_visible()
    expect(executor).to_contain_text("max_jobs")
    expect(executor.locator(".executor-config-source").first).to_be_visible()
    assert executor.evaluate("el => !!el.closest('.executor-details')")
    page.locator('#executor-subtabs [data-executor-subtab="overview"]').click()
    assert not executor.inner_text()

    h.assert_clean_browser()
