"""Keyed React Executor MCP/Skills management lists and role boundaries."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(150),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_executor_management_real_and_keyed_browser(
    browser_harness: BrowserHarness,
) -> None:
    h = browser_harness
    h.login()
    h.wait_executor_online()
    page = h.page
    h.navigate("executors")
    page.locator('[data-executor-subtab="mcp"]').click()
    actual_mcp = page.locator("#executor-mcp-body")
    expect(actual_mcp.locator(".executor-manage-group")).to_have_count(1)
    expect(actual_mcp).to_contain_text("Control")
    assert page.evaluate(
        "typeof window.WorkgateReactExecutorManagement?.mount === 'function'"
    )
    page.locator(f'#executor-list [data-selection="{h.executor_id}"]').click()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(actual_mcp.locator(".executor-manage-group h4")).to_have_text(
        ["Executor", "Control"]
    )
    page.locator('[data-executor-subtab="skills"]').click()
    expect(
        page.locator("#executor-skills-body .executor-manage-group h4")
    ).to_have_text(["Executor", "User Global", "Control"])

    # Synthetic models isolate reconciliation, authorization and XSS from
    # whatever MCP/Skills happen to be installed in the test environment.
    page.evaluate("""() => {
      const parent = document.getElementById('app-main');
      const mcp = document.createElement('div'); mcp.id = 'management-mcp-contract';
      const skills = document.createElement('div'); skills.id = 'management-skills-contract';
      parent.append(mcp, skills);
      window.__mgmtView = window.WorkgateReactExecutorManagement.mount(mcp, skills);
      window.__mgmtXss = 0;
      window.__mgmtCalls = [];
      window.__mgmtModel = {
        scope: 'executor-1',
        mcp: [
          {key: 'executor-mcp', title: 'Executor', rows: [
            {title: 'same', detail: 'stdio', status: 'Enabled',
              manage: () => window.__mgmtCalls.push('first')},
            {title: '<img src=x onerror="window.__mgmtXss=1">', status: 'Disabled'},
          ]},
          {key: 'control-mcp', title: 'Control', rows: [
            {title: 'same', detail: 'http', status: 'Overridden',
              authMode: 'secret', authState: 'required'},
            {title: 'service', status: 'Enabled', authMode: 'oauth',
              authState: 'authorized'},
          ]},
        ],
        skills: [
          {key: 'executor-skill', title: 'Executor', rows: [{title: 'Offline', notice: 'warning'}]},
          {key: 'user-global-skill', title: 'User Global', rows: [{title: 'global', status: 'Effective'}]},
          {key: 'control-skill', title: 'Control', rows: [{title: 'Access denied', notice: 'error'}]},
        ],
      };
      window.__mgmtView.render(window.__mgmtModel);
    }""")
    mcp = page.locator("#management-mcp-contract")
    skills = page.locator("#management-skills-contract")
    expect(mcp.locator(".executor-manage-group h4")).to_have_text(
        ["Executor", "Control"]
    )
    expect(
        mcp.locator('.executor-manage-row[data-state="overridden"]')
    ).to_contain_text("Secret required")
    expect(
        mcp.locator('.executor-manage-row[data-auth="authorized"]')
    ).to_contain_text("✓ Authorized")
    expect(
        skills.locator('.executor-notice[data-tone="warning"]')
    ).to_have_text("Offline")
    expect(skills.locator('[role="alert"]')).to_have_text("Access denied")
    expect(
        skills.locator('.executor-manage-pill[data-enabled="true"]')
    ).to_have_text("Effective")
    assert mcp.locator("img, script").count() == 0
    assert page.evaluate("window.__mgmtXss") == 0
    first = mcp.locator('[data-source-key="executor-mcp"]')
    button = first.get_by_role("button", name="Manage")
    button.focus()
    expect(button).to_be_focused()
    page.evaluate("""() => {
      window.__mgmtNode = document.querySelector('#management-mcp-contract [data-source-key="executor-mcp"]');
      window.__mgmtButton = window.__mgmtNode.querySelector('button');
      window.__mgmtModel.mcp[0].rows[0] = {
        title: 'same', detail: 'stdio', status: 'Disabled',
        manage: () => window.__mgmtCalls.push('updated')
      };
      window.__mgmtView.render(window.__mgmtModel);
    }""")
    expect(button).to_be_focused()
    assert page.evaluate(
        "window.__mgmtNode === document.querySelector('#management-mcp-contract [data-source-key=\"executor-mcp\"]')"
    )
    assert page.evaluate("window.__mgmtButton === document.activeElement")
    expect(
        first.locator('.executor-manage-row[data-state="disabled"]')
    ).to_have_count(2)
    button.click()
    assert page.evaluate("window.__mgmtCalls") == ["updated"]
    page.evaluate("""() => {
      window.__mgmtModel.mcp.reverse();
      window.__mgmtView.render(window.__mgmtModel);
    }""")
    expect(mcp.locator(".executor-manage-group h4")).to_have_text(
        ["Control", "Executor"]
    )
    assert page.evaluate(
        "window.__mgmtNode === document.querySelector('#management-mcp-contract [data-source-key=\"executor-mcp\"]')"
    )
    # Revoking a source's management permission removes its existing action.
    page.evaluate("""() => {
      const executor = window.__mgmtModel.mcp.find(group => group.key === 'executor-mcp');
      executor.rows[0] = {...executor.rows[0], manage: undefined};
      window.__mgmtView.render(window.__mgmtModel);
    }""")
    expect(mcp.get_by_role("button", name="Manage")).to_have_count(0)
    # Selecting another Executor invalidates the old row identity and callbacks.
    page.evaluate("""() => {
      window.__mgmtModel = {...window.__mgmtModel, scope:'executor-2', mcp:[
        {key:'executor-mcp', title:'Executor', rows:[
          {title:'same', status:'Enabled'},
        ]},
      ]};
      window.__mgmtView.render(window.__mgmtModel);
    }""")
    assert not page.evaluate(
        "window.__mgmtNode === document.querySelector('#management-mcp-contract [data-source-key=\"executor-mcp\"]')"
    )
    expect(mcp.get_by_role("button", name="Manage")).to_have_count(0)
    h.assert_clean_browser()
