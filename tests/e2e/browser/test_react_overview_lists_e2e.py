"""Authenticated Chromium contract for React Dashboard Overview lists."""

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


def test_react_overview_lists_preserve_actions_and_focus(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    harness.navigate("overview")

    assert page.evaluate(
        "typeof window.WorkgateReactOverviewLists?.mount === 'function'"
    )
    expect(
        page.locator("#dashboard-overview-lists .dashboard-section")
    ).to_have_count(3)
    selected = page.locator(
        f'#dashboard-machine-list [data-executor-id="{harness.executor_id}"]'
    )
    expect(selected).to_have_attribute("aria-current", "true")
    selected.click()
    expect(page.locator("#page-title")).to_have_text("Overview")
    page.get_by_role("button", name="Manage executors →").click()
    expect(page.locator("#page-title")).to_have_text("Executors")
    harness.navigate("overview")
    page.get_by_role("button", name="View audit →").click()
    expect(page.locator("#page-title")).to_have_text("Audit")
    harness.navigate("overview")
    page.get_by_role("button", name="View all →").click()
    expect(page.locator("#page-title")).to_have_text("Tasks")
    harness.navigate("overview")

    # Exercise rendered data and callbacks independently of live poll timing.
    page.evaluate("""() => {
      const host = document.createElement('div');
      host.id = 'overview-list-contract';
      host.className = 'dashboard-executors-row';
      document.getElementById('app-main').append(host);
      window.__overviewActions = [];
      window.__overviewTest = window.WorkgateReactOverviewLists.mount(host, {
        navigate: view => window.__overviewActions.push(['navigate', view]),
        selectExecutor: id => window.__overviewActions.push(['executor', id]),
        openTask: id => window.__overviewActions.push(['task', id]),
      });
      window.__overviewTest.renderExecutors([
        {executor_id:'node-a',name:'alpha',status:'online',runtime:{workgate_version:'5.0.0'}},
        {executor_id:'node-b',name:'beta',status:'offline'},
      ], 'node-a', false);
      window.__overviewTest.renderAlerts([
        {severity:'critical',title:'Sample warning',detail:'Needs review',node:'node-a'},
        {severity:'info',title:'Success',detail:'Healthy',node:'node-b'},
      ], 'No active alerts.');
      window.__overviewTest.renderTasks([
        {task_id:'task-one',objective:'Focus remains',status:'running'},
        {task_id:'task-done',objective:'Hidden',status:'completed'},
        {task_id:'task-two',objective:'Second',status:'queued'},
      ]);
    }""")
    root = page.locator("#overview-list-contract")
    expect(root.locator("#dashboard-machine-list button")).to_have_count(2)
    expect(root.get_by_role("button", name="Select alpha")).to_have_attribute(
        "aria-current", "true"
    )
    expect(root.get_by_role("button", name="Select beta")).to_be_disabled()
    expect(
        root.locator("#dashboard-alerts .dashboard-list-item")
    ).to_have_count(2)
    expect(
        root.locator(".dashboard-list-critical .dashboard-event-icon")
    ).to_have_text("!")
    expect(
        root.locator("#dashboard-task-list .dashboard-task-row")
    ).to_have_count(2)
    row = root.locator('[data-task-id="task-one"]')
    row.focus()
    expect(row).to_be_focused()
    page.evaluate("""() => {
      window.__focusedOverviewTask = document.activeElement;
      window.__overviewTest.renderSelection('node-b', false);
      window.__overviewTest.renderAlerts([], 'Alerts for node-b are not loaded.');
    }""")
    expect(root.get_by_role("button", name="Select beta")).to_have_attribute(
        "aria-current", "true"
    )
    expect(root.locator("#dashboard-alerts .empty-state")).to_have_text(
        "Alerts for node-b are not loaded."
    )
    assert row.evaluate("element => element === window.__focusedOverviewTask")
    expect(row).to_be_focused()
    row.click()
    expect(
        root.locator("#dashboard-task-list .dashboard-task-row")
    ).to_have_count(2)
    page.evaluate("""() => window.__overviewTest.renderExecutors([
      {executor_id:'node-a',name:'alpha',status:'online'},
      {executor_id:'node-b',name:'beta',status:'online'}
    ], 'node-a', false)""")
    root.get_by_role("button", name="Select beta").click()
    assert page.evaluate("window.__overviewActions") == [
        ["task", "task-one"],
        ["executor", "node-b"],
    ]
    page.evaluate("""() => {
      window.__overviewTest.renderTasks([]);
      window.__overviewTest.renderExecutors([], '', false);
    }""")
    expect(root.locator("#dashboard-task-list .empty-state")).to_have_text(
        "No active tasks."
    )
    expect(root.locator("#dashboard-machine-list .empty-state")).to_have_text(
        "No executors paired."
    )
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
