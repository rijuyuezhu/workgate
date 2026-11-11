"""Configuration React view preserves scoped navigation, staging, and safe text."""

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


def test_react_configuration_real_and_staged(
    browser_harness: BrowserHarness,
) -> None:
    h = browser_harness
    h.login()
    h.wait_executor_online()
    page = h.page
    h.navigate("executors")
    page.locator('[data-executor-subtab="config"]').click()
    body = page.locator("#executor-config-body")
    expect(body.locator(".executor-config-row").first).to_be_visible()
    assert page.evaluate(
        "typeof window.WorkgateReactConfiguration?.mount === 'function'"
    )
    # Changing views must immediately clear potentially sensitive values.
    page.locator('[data-executor-subtab="overview"]').click()
    expect(body).to_be_empty()

    page.evaluate("""() => {
      const host = document.createElement('div');
      host.id = 'react-config-contract';
      document.getElementById('app-main').append(host);
      window.__configView = window.WorkgateReactConfiguration.mount(host);
      window.__configSaved = null;
      window.__configXss = 0;
      window.__configModel = {
        generation: 1, state: 'ready', data: {
          status: 'running', editable: true, config_path: '/tmp/safe.json',
          fields: [
            {name: 'max_jobs', section: 'Safety', type: 'int', file_present: false,
              default_value: 10, effective_value: 11},
            {name: '<img src=x onerror="window.__configXss=1">', section: 'Safety',
              type: 'str', file_present: true, file_value: '<script>bad</script>',
              default_value: 'safe', effective_value: 'safe'},
            {name: 'enabled', section: 'General', type: 'bool', file_present: true,
              file_value: true, default_value: true, effective_value: true}
          ]
        },
        events: {
          retry: () => {},
          save: async changes => {window.__configSaved = changes;},
          apply: async () => false
        }
      };
      window.__configView.render(window.__configModel);
    }""")
    root = page.locator("#react-config-contract")
    expect(root.locator(".executor-config-row")).to_have_count(3)
    expect(root.locator(".executor-config-section")).to_have_count(2)
    assert root.locator("img, script").count() == 0
    assert page.evaluate("window.__configXss") == 0
    row = root.locator(".executor-config-row").filter(
        has=page.locator('.executor-config-name:text-is("max_jobs")')
    )
    expect(row).to_be_visible()
    root.locator(".executor-config-section").first.locator("summary").click()
    expect(row).to_be_hidden()
    # Filtering expands matching sections and restores the user's choice.
    non_default = (
        root.locator(".executor-config-filter")
        .filter(has_text="Non-default")
        .locator("input")
    )
    non_default.check()
    expect(row).to_be_visible()
    non_default.uncheck()
    expect(row).to_be_hidden()
    root.locator(".executor-config-section").first.locator("summary").click()
    expect(row).to_be_visible()
    row.get_by_role("button", name="Edit").click()
    editor = row.locator("input.executor-config-input")
    expect(editor).to_be_focused()
    editor.fill("1.5")
    row.get_by_role("button", name="Set", exact=True).click()
    expect(root).to_contain_text("Invalid value")
    editor.fill("32")
    row.get_by_role("button", name="Set", exact=True).click()
    expect(row).to_have_class("executor-config-row executor-config-changed")
    expect(
        root.locator(".executor-config-filter").filter(has_text="Only changed")
    ).to_contain_text("Only changed (1)")
    changed_only = (
        root.locator(".executor-config-filter")
        .filter(has_text="Only changed")
        .locator("input")
    )
    changed_only.check()
    expect(root.locator(".executor-config-row:visible")).to_have_count(1)
    changed_only.uncheck()
    root.get_by_role("button", name="Save changes").click()
    expect(root.locator(".executor-config-actions").first).to_contain_text(
        "Save changes"
    )
    expect(
        page.locator("#react-config-contract .executor-config-transition").first
    ).to_contain_text("32")
    assert page.evaluate("window.__configSaved") == {
        "max_jobs": {"op": "set", "value": 32}
    }
    page.evaluate("window.__configView.render({generation: 2, state: 'blank'})")
    expect(root).to_be_empty()
    page.evaluate(
        "window.__configView.render({generation: 3, state: 'offline', message: 'Executor offline'})"
    )
    expect(root).to_contain_text("Executor offline")
    expect(root.get_by_role("button", name="Retry")).to_have_count(0)
    page.evaluate("""() => window.__configView.render({generation: 3, state: 'offline',
      message: 'Executor offline', events: {retry: () => window.__configRetried = true}})""")
    root.get_by_role("button", name="Retry").click()
    assert page.evaluate("window.__configRetried") is True
    page.evaluate("window.__configView.render({generation: 4, state: 'blank'})")
    expect(root).to_be_empty()
    h.assert_clean_browser()
