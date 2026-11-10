"""Authenticated Chromium contracts for the React Executor fleet inventory."""

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


def test_react_executor_inventory_navigation_and_snapshot(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    page.goto(f"{harness.base_url}/ui#executors")
    expect(page.locator("#executor-list .executor-row")).to_have_count(2)
    expect(
        page.locator("#executor-inventory-summary #executor-total")
    ).to_have_text("1")
    expect(
        page.locator("#executor-inventory-summary #executor-online")
    ).to_have_text("1")
    assert page.evaluate(
        "typeof window.WorkgateReactExecutorInventory?.mount === 'function'"
    )
    control = page.locator('#executor-list [data-selection="__control__"]')
    machine = page.locator(
        f'#executor-list [data-selection="{harness.executor_id}"]'
    )
    expect(control).to_have_attribute("aria-pressed", "true")
    machine.click()
    expect(machine).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#executor-detail-id")).to_have_text(
        harness.executor_id
    )
    control.click()
    expect(control).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#executor-detail-name")).to_have_text("Control")

    # Render independent mixed snapshots, preserving keys/focus during updates.
    page.evaluate("""() => {
      const list = document.createElement('nav');
      list.id = 'fleet-contract';
      list.className = 'executor-list';
      const summary = document.createElement('div');
      summary.id = 'fleet-contract-summary';
      summary.className = 'inventory-summary';
      document.getElementById('app-main').append(summary, list);
      window.__fleetActions = [];
      window.__fleetView = window.WorkgateReactExecutorInventory.mount(
        list, summary, id => window.__fleetActions.push(id)
      );
      window.__fleetRows = [
        {key:'__control__',name:'Control',label:'Control plane',status:'available'},
        {key:'ex-online',name:'alpha',label:'v6.0',status:'online',resources:'CPU 20% · MEM 30%'},
        {key:'ex-drain',name:'beta',label:'v5.0',status:'draining'},
        {key:'ex-off',name:'gamma',label:'Executor',status:'offline'},
        {key:'ex-revoked',name:'delta',label:'Executor',status:'revoked'},
      ];
      window.__fleetView.renderList(window.__fleetRows, '__control__');
      window.__fleetView.renderSummary(4, 1, 1);
    }""")
    list = page.locator("#fleet-contract")
    summary = page.locator("#fleet-contract-summary")
    expect(list.locator(".executor-control-group .executor-row")).to_have_count(
        1
    )
    expect(list.locator(".executor-peer-rows .executor-row")).to_have_count(4)
    expect(
        list.locator('[data-selection="ex-drain"] .executor-band-state')
    ).to_have_attribute("data-status", "draining")
    expect(
        list.locator('[data-selection="ex-revoked"] .executor-band-state')
    ).to_have_attribute("data-status", "revoked")
    expect(
        list.locator('[data-selection="ex-online"] .executor-band-resources')
    ).to_have_text("CPU 20% · MEM 30%")
    expect(summary.locator("#executor-total")).to_have_text("4")
    expect(summary.locator("#executor-online")).to_have_text("1")
    expect(summary.locator("#executor-revoked")).to_have_text("1")
    expect(summary.locator("#executor-revoked-summary")).to_be_visible()
    online = list.locator('[data-selection="ex-online"]')
    online.focus()
    expect(online).to_be_focused()
    page.evaluate("""() => {
      window.__savedFleet = document.activeElement;
      window.__fleetChanges = 0;
      new MutationObserver(records => {
        for (const record of records) window.__fleetChanges +=
          record.type === 'attributes' ? 1 : record.addedNodes.length + record.removedNodes.length;
      }).observe(document.getElementById('fleet-contract'), {
        subtree: true, attributes: true, childList: true
      });
      window.__fleetView.renderList([...window.__fleetRows], '__control__');
    }""")
    page.wait_for_timeout(100)
    assert page.evaluate("window.__fleetChanges") == 0
    assert online.evaluate("el => el === window.__savedFleet")
    expect(online).to_be_focused()
    page.evaluate("""() => {
      window.__fleetRows[1] = {
        ...window.__fleetRows[1], name:'renamed-in-poll', status:'draining', resources:''
      };
      window.__fleetView.renderList(window.__fleetRows, '__control__');
    }""")
    expect(online.locator(".executor-band-name")).to_have_text(
        "renamed-in-poll"
    )
    expect(online.locator(".executor-band-state")).to_have_attribute(
        "data-status", "draining"
    )
    expect(online.locator(".executor-band-resources")).to_be_empty()
    assert online.evaluate("el => el === window.__savedFleet")
    expect(online).to_be_focused()
    online.click()
    assert page.evaluate("window.__fleetActions") == ["ex-online"]
    page.evaluate("""() => {
      window.__fleetView.renderList([window.__fleetRows[0]], '__control__');
      window.__fleetView.renderSummary(0, 0, 0);
    }""")
    expect(list.locator(".executor-peer-group")).to_be_hidden()
    expect(list.locator(".executor-row")).to_have_count(1)
    expect(summary.locator("#executor-total")).to_have_text("0")
    expect(summary.locator("#executor-revoked-summary")).to_be_hidden()
    page.evaluate("""() => window.__fleetView.renderSummary(null,null,null)""")
    expect(summary.locator("#executor-total")).to_have_text("—")
    expect(summary.locator("#executor-online")).to_have_text("—")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
