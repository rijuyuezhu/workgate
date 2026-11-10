"""React-owned top navigation, with WorkGate owning URL/history and mobile menu."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(120),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_react_primary_navigation_preserves_routing_and_mobile_menu(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page

    nav = page.locator("#app-navigation")
    expect(nav).to_have_attribute("aria-label", "Primary navigation")
    items = nav.locator(".nav-item[data-view]")
    expect(items).to_have_count(6)
    assert page.evaluate(
        "typeof window.WorkgateReactNavigation?.mount === 'function'"
    )
    assert page.evaluate(
        "typeof window.WorkgateReactFiles?.mount === 'function'"
    )
    expect(nav.locator('[aria-current="page"]')).to_have_count(1)

    harness.navigate("files")
    expect(page).to_have_url(f"{harness.base_url}/ui#files")
    harness.navigate("audit")
    expect(nav.locator('[data-view="audit"]')).to_have_attribute(
        "aria-current", "page"
    )
    page.go_back(wait_until="domcontentloaded")
    expect(page.locator("#page-title")).to_have_text("Files")
    expect(nav.locator('[data-view="files"]')).to_have_attribute(
        "aria-current", "page"
    )
    page.go_forward(wait_until="domcontentloaded")
    expect(page.locator("#page-title")).to_have_text("Audit")

    # All page shortcuts use the same existing WorkGate route handler.
    page.locator(".brand-block").click()
    expect(page.locator("#page-title")).to_have_text("Overview")
    page.get_by_role("button", name="Manage executors →").click()
    expect(page.locator("#page-title")).to_have_text("Executors")

    page.set_viewport_size({"width": 390, "height": 844})
    menu = page.locator("#nav-toggle")
    expect(menu).to_be_visible()
    expect(nav).to_be_hidden()
    menu.click()
    expect(menu).to_have_attribute("aria-expanded", "true")
    expect(nav).to_be_visible()
    nav.locator('[data-view="tasks"]').click()
    expect(page.locator("#page-title")).to_have_text("Tasks")
    expect(menu).to_have_attribute("aria-expanded", "false")
    expect(nav).to_be_hidden()
    expect(nav.locator('[aria-current="page"]')).to_have_count(1)

    menu.click()
    page.keyboard.press("Escape")
    expect(nav).to_be_hidden()
    expect(menu).to_be_focused()

    menu.click()
    page.mouse.click(5, 610)
    expect(nav).to_be_hidden()
    harness.assert_clean_browser()
