"""Keyed React MCP editor results preserve real auth actions and safe text."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(160),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_react_mcp_editor_real_auth_and_discovery(
    browser_harness: BrowserHarness,
) -> None:
    h = browser_harness
    h.login()
    h.wait_executor_online()
    page = h.page
    h.navigate("executors")
    page.locator('[data-executor-subtab="mcp"]').click()
    page.locator("#executor-mcp-add").click()
    page.locator("#executor-integration-owner").select_option("control")
    page.locator("#executor-integration-name").fill("react-secret-editor")
    page.locator("#executor-integration-url").fill("https://example.org/mcp")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    row = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="react-secret-editor"
    )
    expect(row).to_be_visible()
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-mcp-secret-list")).to_contain_text(
        "No private secrets saved for this MCP."
    )
    page.locator("#executor-mcp-secret-details summary").click()
    page.locator("#executor-mcp-secret-name").fill("test_key")
    page.locator("#executor-mcp-secret-value").fill(
        "PRIVATE_VALUE_NEVER_DISPLAY"
    )
    page.locator("#executor-mcp-secret-set").click()
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "stored"
    )
    secret = page.locator("#executor-mcp-secret-list .executor-auth-secret-row")
    expect(secret).to_have_count(1)
    expect(secret.locator("code")).to_have_text("test_key")
    expect(secret).to_contain_text("Value hidden")
    assert "PRIVATE_VALUE_NEVER_DISPLAY" not in secret.inner_text()
    expect(secret.get_by_role("button", name="Remove")).to_be_enabled()
    page.locator("#executor-mcp-advanced-settings > summary").click()
    page.locator("#executor-integration-test").click()
    expect(page.locator("#executor-integration-test-result")).to_contain_text(
        "MCP unavailable", timeout=20000
    )
    expect(secret.get_by_role("button", name="Remove")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    secret.get_by_role("button", name="Remove").click()
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "removed"
    )
    expect(page.locator("#executor-mcp-secret-list")).to_contain_text(
        "No private secrets saved for this MCP."
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-mcp-body")).not_to_contain_text(
        "react-secret-editor"
    )
    h.assert_clean_browser()


def test_react_mcp_editor_keyed_escape_and_busy_contract(
    browser_harness: BrowserHarness,
) -> None:
    h = browser_harness
    h.login()
    page = h.page
    assert page.evaluate(
        "typeof window.WorkgateReactExecutorMcpEditor?.mount === 'function'"
    )
    page.evaluate("""() => {
      const parent = document.getElementById('app-main');
      const a = document.createElement('div'); a.id = 'mcp-secret-contract';
      const b = document.createElement('div'); b.id = 'mcp-test-contract'; b.className = 'muted';
      parent.append(a,b);
      window.__mcpDeletes = [];
      window.__mcpView = window.WorkgateReactExecutorMcpEditor.mount(
        a, b, key => window.__mcpDeletes.push(key));
      window.__mcpView.renderSecrets(['alpha','<img src=x onerror=alert(1)>'],false);
      window.__mcpView.renderTest('2 tools discovered (none called)','success',[
        {name:'<script>bad</script>',description:'literal <b>bold</b>'},
        {name:'tool2',description:'good'}
      ]);
    }""")
    secrets = page.locator("#mcp-secret-contract")
    results = page.locator("#mcp-test-contract")
    expect(secrets.locator(".executor-auth-secret-row")).to_have_count(2)
    assert secrets.locator("img,script").count() == 0
    expect(results.locator("div")).to_have_count(2)
    assert results.locator("script,b").count() == 0
    expect(results).to_have_attribute("data-tone", "success")
    button = secrets.locator(".executor-auth-secret-row").first.get_by_role(
        "button"
    )
    button.focus()
    page.evaluate("""() => {
      window.__mcpButton = document.querySelector('#mcp-secret-contract button');
      window.__mcpView.renderSecrets(['alpha','changed'],false);
    }""")
    expect(button).to_be_focused()
    assert page.evaluate("document.activeElement === window.__mcpButton")
    page.evaluate("window.__mcpView.renderSecrets(['alpha','changed'],true)")
    expect(button).to_be_disabled()
    page.evaluate("window.__mcpView.renderSecrets(['alpha','changed'],false)")
    button.click()
    assert page.evaluate("window.__mcpDeletes") == ["alpha"]
    page.evaluate("window.__mcpView.renderSecrets([],false)")
    expect(secrets).to_contain_text("No private secrets saved")
    page.evaluate("window.__mcpView.renderTest('Cannot connect','error')")
    expect(results).to_have_attribute("role", "alert")
    expect(results).to_have_attribute("data-tone", "error")
    page.evaluate("window.__mcpView.renderTest('')")
    expect(results).to_be_empty()
    assert results.get_attribute("data-tone") is None
    h.assert_clean_browser()
