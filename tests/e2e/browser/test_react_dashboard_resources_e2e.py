"""Deterministic Chromium contract for the React Dashboard resource cards."""

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


def test_react_dashboard_resources_format_and_reset(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    harness.navigate("overview")

    assert page.evaluate(
        "typeof window.WorkgateReactDashboardResources?.mount === 'function'"
    )
    expect(
        page.locator(
            "#dashboard-panel .dashboard-summary .dashboard-metric-card"
        )
    ).to_have_count(4)
    expect(
        page.locator(
            "#dashboard-panel .dashboard-summary [data-resource='network']"
        )
    ).to_be_visible()

    # Executor details used to clone Dashboard's HTML template. Ensure the
    # shared React renderer still displays those metrics after the migration.
    harness.navigate("executors")
    row = page.locator(
        f'#executor-list .executor-row[data-selection="{harness.executor_id}"]'
    )
    expect(row).to_be_visible()
    row.click()
    metrics = page.locator("#executor-overview-metrics")
    expect(metrics.locator(".dashboard-metric-card")).to_have_count(
        4, timeout=12_000
    )
    expect(metrics.locator("#executor-cpu")).not_to_have_text("—")
    expect(metrics.locator(".dashboard-live")).to_have_count(0)
    expect(page.locator("#dashboard-cpu")).to_have_count(1)
    harness.navigate("overview")

    # Use a separate React root to test synthetic data deterministically. The
    # WorkGate Dashboard's real authenticated polling keeps its own sole root.
    page.evaluate("""() => {
      const host = document.createElement('div');
      host.id = 'resource-contract-test';
      host.className = 'dashboard-summary';
      document.querySelector('#app-main').append(host);
      window.__resourceTestView = window.WorkgateReactDashboardResources.mount(host);
      window.__resourceTestView.render({
        cpu_percent: 42.5, cpu_count: 8, load_1m: 1.25,
        memory_percent: 62.5, memory_used_bytes: 1048576,
        memory_total_bytes: 4194304,
        disk_percent: 25, disk_used_bytes: 536870912,
        disk_total_bytes: 1073741824,
        network_rx_bps: 1024, network_tx_bps: 3072
      }, [
        {cpu: 0, memory: 20, disk: null},
        {cpu: 50, memory: 60, disk: 25}
      ], 'Live');
    }""")
    root = page.locator("#resource-contract-test")
    expect(root.locator(".dashboard-metric-card")).to_have_count(4)
    expect(root.locator("#dashboard-cpu")).to_have_text("42.5%")
    expect(root.locator("#dashboard-memory")).to_have_text("62.5%")
    expect(root.locator("#dashboard-disk")).to_have_text("25.0%")
    expect(root.locator("#dashboard-cpu-foot-load")).to_have_text("Load 1.25")
    expect(root.locator("#dashboard-cpu-foot-cores")).to_have_text("8 cores")
    expect(root.locator("#dashboard-memory-foot-used")).to_have_text(
        "1.00 MiB used"
    )
    expect(root.locator("#dashboard-network-down")).to_have_text("1.00 KiB")
    expect(root.locator("#dashboard-network-up")).to_have_text("3.00 KiB")
    expect(root.locator("#dashboard-network")).to_have_text("4.00 KiB/s")
    expect(root.locator("#dashboard-cpu-trend polyline")).to_have_count(1)
    expect(root.locator("#dashboard-memory-trend polyline")).to_have_count(1)
    expect(root.locator("#dashboard-disk-trend polyline")).to_have_count(0)
    assert (
        root.locator("#dashboard-network-rx-bar").evaluate(
            "el => el.style.width"
        )
        == "25%"
    )
    assert (
        root.locator("#dashboard-network-tx-bar").evaluate(
            "el => el.style.width"
        )
        == "75%"
    )

    page.evaluate("""() => window.__resourceTestView.render({
      cpu_percent: 0, network_rx_bps: 0, network_tx_bps: 0
    }, [], 'Unavailable')""")
    expect(root.locator("#dashboard-cpu")).to_have_text("0.0%")
    expect(root.locator("#dashboard-memory")).to_have_text("—")
    expect(root.locator("#dashboard-network-down")).to_have_text("0 B")
    expect(root.locator("#dashboard-cpu-trend polyline")).to_have_count(0)
    expect(root.locator(".dashboard-live").first).to_have_text("Unavailable")
    assert (
        root.locator("#dashboard-network-rx-bar").evaluate(
            "el => el.style.width"
        )
        == "0%"
    )

    page.set_viewport_size({"width": 390, "height": 844})
    expect(root.locator(".dashboard-metric-card")).to_have_count(4)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
