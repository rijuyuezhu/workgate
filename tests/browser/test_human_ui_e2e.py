import pytest

from tests.browser.harness import BrowserHarness
from tests.browser.scenario_auth_dashboard import run_auth_dashboard
from tests.browser.scenario_executors import run_executor_admin
from tests.browser.scenario_files_todos_audit import run_files_todos_audit
from tests.browser.scenario_opentui_responsive import run_opentui_responsive
from tests.browser.scenario_terminals_remote import run_terminals_remote

pytestmark = [pytest.mark.browser, pytest.mark.timeout(240)]


def test_human_ui_real_chromium(browser_harness: BrowserHarness) -> None:
    run_auth_dashboard(browser_harness)
    run_executor_admin(browser_harness)
    run_files_todos_audit(browser_harness)
    run_terminals_remote(browser_harness)
    run_opentui_responsive(browser_harness)
    browser_harness.assert_clean_browser()
