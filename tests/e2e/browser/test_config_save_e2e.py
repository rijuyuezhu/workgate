"""Real OAuth WebUI: edit actual role files, preserve effective values and parent PIDs."""

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.browser.harness import BrowserHarness


def test_config_save_is_file_only_with_role_ownership(tmp_path: Path):
    if os.environ.get("WORKGATE_BROWSER_E2E") != "1":
        pytest.skip("Chromium suite only")
    artifacts = (
        Path(
            os.environ.get("WORKGATE_BROWSER_ARTIFACTS", "browser-artifacts")
        ).resolve()
        / "config-save"
    )
    with sync_playwright() as playwright:
        h = BrowserHarness.start(
            tmp_path, artifacts, playwright, editable_configs=True
        )
        failed = True
        try:
            h.login()
            h.wait_executor_online()
            control_pid, executor_pid = h.server.pid, h.executor.pid
            page = h.page
            h.navigate("executors")
            page.locator("#executor-control-config-open").click()
            control = page.locator("#executor-control-config-body")
            row = control.locator(".executor-config-row").filter(
                has=page.locator('.executor-config-name:text-is("log_level")')
            )
            # Every role-owned field appears, even when missing from YAML.
            port = control.locator(".executor-config-row").filter(
                has=page.locator('.executor-config-name:text-is("port")')
            )
            expect(port.locator(".executor-config-primary code")).to_have_text(
                "unset"
            )
            expect(port.locator(".executor-config-file-value")).to_contain_text(
                "Running"
            )
            only_changed = control.locator(".executor-config-filter input")
            only_changed.check()
            expect(control.locator(".executor-config-empty")).to_have_text(
                "No unsaved changes"
            )
            only_changed.uncheck()
            row.get_by_role("button", name="Edit").click()
            row.locator("select.executor-config-input").select_option("info")
            row.get_by_role("button", name="Set", exact=True).click()
            expect(row).to_have_class(re.compile("executor-config-changed"))
            expect(row.locator(".executor-config-primary code")).to_have_text(
                "info"
            )
            expect(row.locator(".executor-config-original")).to_have_text(
                "Was warning"
            )
            expect(
                control.locator(".executor-config-filter span")
            ).to_have_text("Only changed (1)")
            only_changed.check()
            expect(
                control.locator(".executor-config-row:visible")
            ).to_have_count(1)
            only_changed.uncheck()
            control.get_by_role("button", name="Save changes").click()
            expect(control).to_contain_text("Saved · Restart to apply")
            assert "log_level: info" in (tmp_path / "control.yaml").read_text()
            assert "warning" in (tmp_path / "control.yaml.bak").read_text()
            assert h.server.pid == control_pid
            page.locator("#executor-control-config-close").click()

            page.locator("#executor-list button").first.click()
            page.locator('[data-executor-subtab="config"]').click()
            executor = page.locator("#executor-config-body")
            expect(executor).to_contain_text("max_jobs")
            row = executor.locator(".executor-config-row").filter(
                has=page.locator('.executor-config-name:text-is("max_jobs")')
            )
            # Open the Safety group to reveal its member rows.
            row.locator("xpath=ancestor::details/summary").click()
            row.get_by_role("button", name="Edit").click()
            row.locator("input.executor-config-input").fill("32")
            row.get_by_role("button", name="Set", exact=True).click()
            expect(row).to_have_class(re.compile("executor-config-changed"))
            executor.get_by_role("button", name="Save changes").click()
            expect(executor).to_contain_text("Saved · Restart to apply")
            assert '"max_jobs": 32' in (tmp_path / "executor.json").read_text()
            assert h.executor.pid == executor_pid
            expect(executor).to_contain_text(
                "18"
            )  # Current Child has not been restarted.
            h.assert_clean_browser()
            failed = False
        finally:
            h.stop(failed=failed)
