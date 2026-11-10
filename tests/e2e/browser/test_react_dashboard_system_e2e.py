"""Authenticated Chromium coverage of the React System details disclosure."""

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


def test_react_dashboard_system_details(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    harness.navigate("overview")

    assert page.evaluate(
        "typeof window.WorkgateReactDashboardSystem?.mount === 'function'"
    )
    disclosure = page.locator(".dashboard-system-disclosure")
    expect(disclosure).not_to_have_attribute("open", "")
    disclosure.locator("summary").click()
    expect(disclosure).to_have_attribute("open", "")
    root = page.locator("#dashboard-system-root")
    expect(root.locator("#dashboard-platform")).not_to_have_text("—")
    expect(root.locator("#dashboard-python")).not_to_have_text("—")

    def synthetic_dashboard(route):
        response = route.fetch()
        payload = response.json()
        data = payload["data"]
        data["generated_at"] = None
        data["version"] = {"platform": "SyntheticOS", "python": "3.14"}
        data["system"].update(
            cpu_percent=0.0,
            memory_percent=None,
            disk_percent=150.0,
            cpu_count=16,
            load_1m=1.234,
            uptime_s=3661,
            memory_used_bytes=1024,
            memory_total_bytes=None,
            disk_used_bytes=1048576,
            disk_total_bytes=4194304,
            network_rx_bps=None,
            network_tx_bps=0.0,
        )
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/dashboard?*", synthetic_dashboard)
    page.reload()
    expect(page.locator("#dashboard-state")).to_contain_text("Updated")
    disclosure = page.locator(".dashboard-system-disclosure")
    expect(disclosure).not_to_have_attribute("open", "")
    disclosure.locator("summary").click()
    root = page.locator("#dashboard-system-root")
    expect(root.locator("#dashboard-generated")).to_have_text(
        "Unknown sample time"
    )
    expect(root.locator("#dashboard-platform")).to_have_text("SyntheticOS")
    expect(root.locator("#dashboard-python")).to_have_text("3.14")
    expect(root.locator("#dashboard-cpu-value")).to_have_text("0.0%")
    expect(root.locator("#dashboard-memory-value")).to_have_text("—")
    expect(root.locator("#dashboard-disk-value")).to_have_text("150.0%")
    assert (
        root.locator("#dashboard-cpu-bar").get_attribute("style")
        == "width: 0%;"
    )
    assert (
        root.locator("#dashboard-disk-bar").get_attribute("style")
        == "width: 100%;"
    )
    expect(root.locator("#dashboard-memory-bar").locator("..")).to_have_class(
        "dashboard-resource-track dashboard-resource-missing"
    )
    expect(root.locator("#dashboard-load")).to_have_text("1.23")
    expect(root.locator("#dashboard-uptime")).to_have_text("1h 1m")
    expect(root.locator("#dashboard-memory-used")).to_have_text("—")
    expect(root.locator("#dashboard-disk-used")).to_have_text(
        "1.00 MiB / 4.00 MiB"
    )
    expect(root.locator("#dashboard-network-rx")).to_have_text("—")
    expect(root.locator("#dashboard-network-tx")).to_have_text("0 B/s")

    # Inventory polling clears details when the active Executor is offline.
    def offline_inventory(route):
        response = route.fetch()
        payload = response.json()
        for item in payload.get("data", {}).get("executors", []):
            if item.get("executor_id") == harness.executor_id:
                item["online"] = False
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/executors", offline_inventory)
    expect(page.locator("#executor-target-online")).to_have_text(
        "0", timeout=12_000
    )
    expect(root.locator("#dashboard-python")).to_have_text("—")
    expect(root.locator("#dashboard-generated")).to_have_text("—")
    expect(disclosure).to_have_attribute("open", "")
    page.unroute("**/api/ui/executors", offline_inventory)
    page.unroute("**/api/ui/dashboard?*", synthetic_dashboard)
    expect(page.locator("#executor-target-online")).to_have_text(
        "1", timeout=12_000
    )
    expect(root.locator("#dashboard-python")).not_to_have_text("—")

    page.set_viewport_size({"width": 390, "height": 844})
    expect(disclosure).to_have_attribute("open", "")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
