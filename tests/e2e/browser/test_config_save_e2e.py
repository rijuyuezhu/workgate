"""Real OAuth WebUI: edit actual role files, preserve effective values and parent PIDs."""

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.browser.harness import BrowserHarness
from workgate.config.roles import CONTROL_SETTING_NAMES, EXECUTOR_SETTING_NAMES


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
            page.locator('[data-executor-subtab="config"]').click()
            control = page.locator("#executor-config-body")
            expect(control.locator(".executor-config-row")).to_have_count(
                len(CONTROL_SETTING_NAMES)
            )
            expect(
                control.locator(".executor-config-default code")
            ).to_have_count(len(CONTROL_SETTING_NAMES))
            assert control.locator("details:not([open])").count() == 0
            row = control.locator(".executor-config-row").filter(
                has=page.locator('.executor-config-name:text-is("log_level")')
            )
            # The configured log level still equals its default, regardless
            # of other fields overridden by the browser test environment.
            expect(row.locator(".executor-config-effective")).to_have_count(0)
            # Every role-owned field appears, even when missing from YAML.
            port = control.locator(".executor-config-row").filter(
                has=page.locator('.executor-config-name:text-is("port")')
            )
            expect(
                port.locator(".executor-config-transition code").first
            ).to_have_text("unset")
            expect(port.locator(".executor-config-default code")).to_have_text(
                "8765"
            )
            # The live port comes from CLI, not from the config file. On a
            # narrow viewport all three labels and their values stay aligned.
            page.set_viewport_size({"width": 390, "height": 844})
            live = port.locator(".executor-config-effective")
            expect(live.locator(".executor-config-live-label")).to_have_text(
                "In use"
            )
            expect(live.locator(".executor-config-source")).to_have_text("cli")
            assert (
                live.locator("code").evaluate(
                    "node => getComputedStyle(node).fontSize"
                )
                == "11px"
            )
            label_x = [
                node.evaluate("element => element.getBoundingClientRect().x")
                for node in (
                    port.locator(
                        ".executor-config-transition .executor-config-mobile-heading"
                    ),
                    port.locator(
                        ".executor-config-default .executor-config-mobile-heading"
                    ),
                    live.locator(".executor-config-live-label"),
                )
            ]
            assert max(label_x) - min(label_x) < 2
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            page.set_viewport_size({"width": 1360, "height": 900})
            non_default = (
                control.locator(".executor-config-filter")
                .filter(has_text="Non-default")
                .locator("input")
            )
            non_default.check()
            # The file explicitly sets log_level to its default (warning).
            expect(row).to_be_hidden()
            non_default.uncheck()
            only_changed = (
                control.locator(".executor-config-filter")
                .filter(has_text="Only changed")
                .locator("input")
            )
            only_changed.check()
            expect(control.locator(".executor-config-empty")).to_have_text(
                "No unsaved changes"
            )
            only_changed.uncheck()
            row.get_by_role("button", name="Edit").click()
            row.locator("select.executor-config-input").select_option("info")
            row.get_by_role("button", name="Set", exact=True).click()
            expect(row).to_have_class(re.compile("executor-config-changed"))
            # Clicking the already active tab must not discard unsaved edits.
            page.locator('[data-executor-subtab="config"]').click()
            expect(row).to_have_class(re.compile("executor-config-changed"))
            expect(row.locator(".executor-config-transition")).to_contain_text(
                "warning→info"
            )
            expect(row.locator(".executor-config-arrow")).to_have_text("→")
            expect(row.locator(".executor-config-default code")).to_have_text(
                "warning"
            )
            expect(
                control.locator(".executor-config-filter").filter(
                    has_text="Only changed"
                )
            ).to_contain_text("Only changed (1)")
            non_default.check()
            expect(row).to_be_visible()
            only_changed.check()
            expect(
                control.locator(".executor-config-row:visible")
            ).to_have_count(1)
            only_changed.uncheck()
            non_default.uncheck()
            only_changed.check()
            expect(
                control.locator(".executor-config-row:visible")
            ).to_have_count(1)
            only_changed.uncheck()
            control.get_by_role("button", name="Save changes").click()
            expect(control).to_contain_text("Saved. Restart to apply.")
            assert "log_level: info" in (tmp_path / "control.yaml").read_text()
            assert "warning" in (tmp_path / "control.yaml.bak").read_text()
            assert h.server.pid == control_pid
            page.locator('[data-executor-subtab="overview"]').click()

            page.locator(
                '#executor-list button:not([data-selection="__control__"])'
            ).first.click()
            page.locator('[data-executor-subtab="config"]').click()
            executor = page.locator("#executor-config-body")
            expect(executor.locator(".executor-config-row")).to_have_count(
                len(EXECUTOR_SETTING_NAMES)
            )
            expect(
                executor.locator(".executor-config-default code")
            ).to_have_count(len(EXECUTOR_SETTING_NAMES))
            assert executor.locator("details:not([open])").count() == 0
            expect(executor).to_contain_text("max_jobs")
            exec_non_default = (
                executor.locator(".executor-config-filter")
                .filter(has_text="Non-default")
                .locator("input")
            )
            exec_non_default.check()
            expect(
                executor.locator(".executor-config-row:visible")
            ).not_to_have_count(len(EXECUTOR_SETTING_NAMES))
            exec_non_default.uncheck()
            row = executor.locator(".executor-config-row").filter(
                has=page.locator('.executor-config-name:text-is("max_jobs")')
            )
            # Open the Safety group to reveal its member rows.
            # Groups are expanded by default, so all fields are accessible.
            expect(row).to_be_visible()
            row.get_by_role("button", name="Edit").click()
            row.locator("input.executor-config-input").fill("32")
            row.get_by_role("button", name="Set", exact=True).click()
            expect(row).to_have_class(re.compile("executor-config-changed"))
            executor.get_by_role("button", name="Save changes").click()
            expect(executor).to_contain_text("Saved. Restart to apply.")
            assert '"max_jobs": 32' in (tmp_path / "executor.json").read_text()
            assert h.executor.pid == executor_pid
            expect(
                row.locator(".executor-config-transition code").first
            ).to_have_text("32")
            expect(row.locator(".executor-config-arrow")).to_be_hidden()
            expect(row.locator(".executor-config-default code")).to_have_text(
                "1000"
            )
            h.assert_clean_browser()
            failed = False
        finally:
            h.stop(failed=failed)
