"""Authenticated Chromium contract for React Dashboard health and status."""

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


def test_react_dashboard_health_and_status(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    harness.navigate("overview")

    assert page.evaluate(
        "typeof window.WorkgateReactDashboardHealth?.mount === 'function'"
    )
    expect(page.locator(".dashboard-health-banner")).to_have_attribute(
        "data-health", "healthy"
    )
    expect(page.locator("#executor-target-online")).to_have_text("1")
    expect(page.locator("#dashboard-state")).to_contain_text("Updated")
    page.get_by_role("button", name="Open Audit records").click()
    expect(page.locator("#page-title")).to_have_text("Audit")
    harness.navigate("overview")

    # A failed authenticated telemetry poll updates React and later recovers.
    def failed_telemetry(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":false,"message":"Synthetic telemetry outage"}',
        )

    page.route("**/api/ui/dashboard?*", failed_telemetry)
    expect(page.locator("#dashboard-health")).to_have_text(
        "unavailable", timeout=12_000
    )
    expect(page.locator("#dashboard-health-detail")).to_contain_text(
        "could not be loaded"
    )
    page.unroute_all(behavior="wait")
    expect(page.locator("#dashboard-health")).to_have_text(
        "All systems operational", timeout=12_000
    )

    # Synthetic snapshots exercise React rendering without changing backend state.
    page.evaluate("""() => {
      const host = document.createElement('div');
      host.id = 'health-contract-test';
      const status = document.createElement('span');
      status.id = 'health-contract-status';
      document.getElementById('app-main').append(host, status);
      window.__healthActions = [];
      window.__healthTest = window.WorkgateReactDashboardHealth.mount(
        host, status, () => window.__healthActions.push('audit')
      );
    }""")
    root = page.locator("#health-contract-test")
    status = page.locator("#health-contract-status")
    expect(root.locator(".dashboard-health-banner")).to_have_attribute(
        "data-health", "unknown"
    )
    expect(root.locator("#executor-target-total")).to_have_text("—")
    expect(status).to_have_text("Not loaded")

    page.evaluate("""() => {
      window.__healthTest.renderTargets([
        {status:'online'}, {status:'offline'}, {status:'online'}
      ]);
      window.__healthTest.renderSnapshot({
        health:'healthy', alerts:[], audit_total_24h:0, audit_failed_24h:0
      });
      window.__healthTest.renderStatus('Updated 12:34');
    }""")
    expect(root.locator("#executor-target-total")).to_have_text("3")
    expect(root.locator("#executor-target-online")).to_have_text("2")
    expect(root.locator("#dashboard-health")).to_have_text(
        "All systems operational"
    )
    expect(root.locator("#dashboard-health-detail")).to_have_text(
        "No active alerts."
    )
    expect(root.locator("#dashboard-audit-total")).to_have_text("0")
    expect(root.locator("#dashboard-audit-detail")).to_have_text(
        "0 failed in 24h"
    )
    expect(status).to_have_text("Updated 12:34")

    audit = root.get_by_role("button", name="Open Audit records")
    audit.focus()
    expect(audit).to_be_focused()
    page.evaluate("""() => {
      window.__focusedHealthAudit = document.activeElement;
      window.__healthTest.renderSnapshot({
        health:'critical', alerts:[{}, {}], audit_total_24h:13,
        audit_failed_24h:2
      });
    }""")
    expect(root.locator(".dashboard-health-banner")).to_have_attribute(
        "data-health", "critical"
    )
    expect(
        root.locator(".dashboard-status-circle .dashboard-health-warning")
    ).to_be_visible()
    expect(root.locator("#dashboard-health")).to_have_text(
        "Critical issue detected"
    )
    expect(root.locator("#dashboard-health-detail")).to_have_text(
        "2 active alerts."
    )
    expect(root.locator("#dashboard-audit-total")).to_have_text("13")
    expect(root.locator("#dashboard-audit-detail")).to_have_text(
        "2 failed in 24h"
    )
    assert audit.evaluate("el => el === window.__focusedHealthAudit")
    expect(audit).to_be_focused()
    audit.click()
    assert page.evaluate("window.__healthActions") == ["audit"]

    page.evaluate("""() => {
      window.__healthTest.renderError('node-1');
      window.__healthTest.renderStatus('Telemetry request failed');
    }""")
    expect(root.locator(".dashboard-health-banner")).to_have_attribute(
        "data-health", "critical"
    )
    expect(root.locator("#dashboard-health-card")).to_have_class(
        "dashboard-card dashboard-health-card dashboard-health-attention"
    )
    expect(root.locator("#dashboard-health")).to_have_text("unavailable")
    expect(root.locator("#dashboard-health-detail")).to_have_text(
        "Telemetry for node-1 could not be loaded"
    )
    expect(status).to_have_text("Telemetry request failed")
    page.evaluate("""() => {
      window.__healthTest.renderSnapshot({health:'attention', alerts:[{}]});
      window.__healthTest.reset();
      window.__healthTest.renderTargets(null);
    }""")
    expect(root.locator(".dashboard-health-banner")).to_have_attribute(
        "data-health", "unknown"
    )
    expect(root.locator("#dashboard-health")).to_have_text("—")
    expect(root.locator("#dashboard-audit-total")).to_have_text("—")
    expect(root.locator("#executor-target-online")).to_have_text("—")

    page.evaluate("""() => window.__healthTest.renderSnapshot({
      health:'attention', alerts:[{}], audit_total_24h:1
    })""")
    expect(root.locator(".dashboard-health-banner")).to_have_attribute(
        "data-health", "attention"
    )
    expect(root.locator("#dashboard-health")).to_have_text("Attention needed")

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
