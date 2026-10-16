import os

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e.browser.harness import BrowserHarness
from tests.e2e.browser.scenario_auth_dashboard import run_auth_dashboard
from tests.e2e.browser.scenario_background_polling import run_background_polling
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
