import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.browser.harness import BrowserHarness
from tests.e2e.browser.scenario_auth_dashboard import run_auth_dashboard
from tests.e2e.browser.scenario_background_polling import run_background_polling
from tests.e2e.browser.scenario_executor_diagnostics import (
    run_executor_diagnostics_ui,
)
from tests.e2e.browser.scenario_executors import run_executor_admin
from tests.e2e.browser.scenario_files_todos_audit import run_files_todos_audit
from tests.e2e.browser.scenario_layout import run_webui_layout
from tests.e2e.browser.scenario_live_workspace import (
    run_live_workspace,
    run_live_workspace_remount,
)
from tests.e2e.browser.scenario_mobile_drilldown import run_mobile_drilldown
from tests.e2e.browser.scenario_native_gui import run_native_gui_manual
from tests.e2e.browser.scenario_remote_terminals import (
    run_remote_terminals,
    run_terminal_action_races,
    run_terminal_switch_races,
)
from tests.e2e.browser.scenario_task_lifecycle import run_task_lifecycle_ui
from tests.e2e.browser.scenario_task_pagination import (
    run_task_implicit_notice_ui,
    run_task_pagination_ui,
    run_task_reselection_ui,
)
from tests.e2e.browser.scenario_terminals import run_terminals

pytestmark = [pytest.mark.browser, pytest.mark.timeout(240)]


def test_human_ui_real_chromium(browser_harness: BrowserHarness) -> None:
    run_auth_dashboard(browser_harness)
    run_webui_layout(browser_harness)
    run_live_workspace(browser_harness)
    run_executor_admin(browser_harness)
    run_files_todos_audit(browser_harness)
    run_task_lifecycle_ui(browser_harness)
    run_background_polling(browser_harness)
    run_terminals(browser_harness)
    run_mobile_drilldown(browser_harness)
    run_native_gui_manual(browser_harness)
    browser_harness.assert_clean_browser()


def test_executor_mcp_auth_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    """Real OAuth-protected UI routes store and erase local MCP secrets privately."""
    browser_harness.login()
    browser_harness.wait_executor_online()
    page = browser_harness.page
    page.set_viewport_size({"width": 1320, "height": 900})
    page.goto(f"{browser_harness.base_url}/ui#executors")
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-add")).to_be_enabled()
    page.locator("#executor-mcp-add").click()
    page.locator("#executor-integration-owner").select_option("executor")
    page.locator("#executor-integration-name").fill("browser-auth-local")
    page.locator("#executor-integration-command").fill("echo")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    row = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="browser-auth-local"
    )
    expect(row).to_be_visible()
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-mcp-auth-management")).to_be_visible()
    expect(page.locator("#executor-mcp-auth-status")).to_contain_text(
        "not_required"
    )
    page.locator("#executor-mcp-secret-name").fill("api_token")
    page.locator("#executor-mcp-secret-value").fill(
        "DO_NOT_ECHO_BROWSER_293747"
    )
    page.locator("#executor-mcp-secret-set").click()
    expect(page.locator("#executor-mcp-secret-list")).to_contain_text(
        "api_token"
    )
    expect(page.locator("#executor-mcp-secret-value")).to_have_value("")
    assert (
        "DO_NOT_ECHO_BROWSER_293747"
        not in page.locator("#executor-integration-dialog").inner_text()
    )
    page.once("dialog", lambda prompt: prompt.accept())
    page.locator("#executor-mcp-secret-list").get_by_role(
        "button", name="Remove"
    ).click()
    expect(page.locator("#executor-mcp-secret-list")).to_contain_text(
        "No private secrets"
    )
    page.once("dialog", lambda prompt: prompt.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    expect(page.locator("#executor-mcp-body")).not_to_contain_text(
        "browser-auth-local"
    )
    # OAuth MCP registration must work without asking the operator to invent
    # the internal credential-store integrationId. Authorization itself is #371.
    page.locator("#executor-mcp-add").click()
    page.locator("#executor-integration-owner").select_option("control")
    page.locator("#executor-integration-name").fill("browser-oauth-http")
    page.locator("#executor-integration-url").fill("https://example.org/mcp")
    page.locator("#executor-integration-auth-mode").select_option("oauth")
    page.locator("#executor-integration-oauth-scopes").fill("read")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    remote = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="browser-oauth-http"
    )
    expect(remote).to_be_visible()
    remote.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-mcp-auth-status")).to_contain_text(
        "unauthorized"
    )
    expect(page.locator("#executor-integration-auth-mode")).to_have_value(
        "oauth"
    )
    page.once("dialog", lambda prompt: prompt.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    expect(page.locator("#executor-mcp-body")).not_to_contain_text(
        "browser-oauth-http"
    )
    browser_harness.assert_clean_browser()


def test_executor_integration_management_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    """Real Browser → authenticated Control → Executor Skill CRUD; no page resets."""
    browser_harness.login()
    browser_harness.wait_executor_online()
    page = browser_harness.page
    page.set_viewport_size({"width": 1360, "height": 900})
    page.goto(f"{browser_harness.base_url}/ui#executors")
    expect(page.locator("#executor-list .executor-row").first).to_be_visible()
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="skills"]').click()
    expect(page.locator("#executor-skills-add")).to_be_enabled(timeout=10000)
    page.locator("#executor-skills-add").click()
    expect(page.locator("#executor-integration-mcp-fields")).to_be_hidden()
    page.locator("#executor-integration-name").fill("browser-managed")
    body = "---\ndescription: Browser test\n---\n# Instructions\n"
    page.locator("#executor-integration-content").fill(body)
    page.locator("#executor-integration-validate").click()
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "valid"
    )
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    row = page.locator("#executor-skills-body .executor-manage-row").filter(
        has_text="browser-managed"
    )
    expect(row).to_be_visible()
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-content")).to_have_value(body)
    page.locator("#executor-integration-toggle").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    expect(row).to_contain_text("Disabled")
    page.wait_for_timeout(4200)
    expect(page.locator('[data-executor-subtab="skills"]')).to_have_attribute(
        "aria-selected", "true"
    )
    row.get_by_role("button", name="Manage").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    expect(page.locator("#executor-skills-body")).not_to_contain_text(
        "browser-managed"
    )
    page.locator('[data-executor-subtab="mcp"]').click()
    page.locator("#executor-mcp-add").click()
    page.locator("#executor-integration-owner").select_option("executor")
    expect(page.locator("#executor-integration-command")).to_be_visible()
    expect(page.locator("#executor-integration-url")).to_be_hidden()
    page.locator("#executor-integration-name").fill("browser-local")
    page.locator("#executor-integration-command").fill("echo")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    local = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="browser-local"
    )
    expect(local).to_be_visible()
    local.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).to_have_value("echo")
    # Editing must preserve disabled state; an unrelated edit cannot turn a
    # previously disabled MCP back on.
    page.locator("#executor-integration-toggle").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    expect(local).to_contain_text("Disabled")
    local.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-save")).to_be_enabled()
    page.locator("#executor-integration-command").fill("printf")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    expect(local).to_contain_text("Disabled")
    local.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).to_have_value(
        "printf"
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-mcp-body")).not_to_contain_text(
        "browser-local"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-body")).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(
        path=str(browser_harness.artifacts / "executor-management-mobile.png")
    )
    browser_harness.assert_clean_browser()


def test_executor_diagnostics_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    browser_harness.login()
    browser_harness.wait_executor_online()
    run_executor_diagnostics_ui(browser_harness)
    browser_harness.assert_clean_browser()


def test_task_pagination_real_chromium(browser_harness: BrowserHarness) -> None:
    browser_harness.login()
    browser_harness.wait_executor_online()
    run_task_pagination_ui(browser_harness)
    browser_harness.assert_clean_browser()


def test_task_reselection_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    browser_harness.login()
    browser_harness.wait_executor_online()
    run_task_reselection_ui(browser_harness)
    browser_harness.assert_clean_browser()


def test_implicit_task_notice_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    browser_harness.login()
    browser_harness.wait_executor_online()
    run_task_implicit_notice_ui(browser_harness)
    browser_harness.assert_clean_browser()


def test_live_workspace_remount_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    run_live_workspace_remount(browser_harness)
    browser_harness.assert_clean_browser()


@pytest.mark.timeout(150)
@pytest.mark.skipif(
    os.environ.get("WORKGATE_BROWSER_E2E") != "1",
    reason="Real Chromium E2E requires the dedicated browser launcher",
)
def test_remote_executor_terminal_real_chromium(tmp_path):
    """Browser -> Control -> StreamHub -> executor B and A, no fallback."""
    artifacts = tmp_path / "remote-browser-artifacts"
    with sync_playwright() as playwright:
        harness = BrowserHarness.start(
            tmp_path, artifacts, playwright, with_remote_executor=True
        )
        failed = True
        try:
            harness.login()
            run_remote_terminals(harness)
            run_terminal_switch_races(harness)
            run_terminal_action_races(harness)
            harness.assert_clean_browser()
            failed = False
        finally:
            harness.stop(failed=failed)
