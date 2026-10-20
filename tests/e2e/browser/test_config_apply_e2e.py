"""Authenticated browser Apply uses an actual supervised Child restart."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.browser.harness import BrowserHarness


def test_browser_applies_control_and_executor_saved_files(tmp_path: Path):
    if os.environ.get("WORKGATE_BROWSER_E2E") != "1" or os.name == "nt":
        pytest.skip("real POSIX Parent + Chromium required")
    artifacts = (
        Path(
            os.environ.get("WORKGATE_BROWSER_ARTIFACTS", "browser-artifacts")
        ).resolve()
        / "config-apply"
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
            page.set_default_timeout(45000)
            h.navigate("executors")
            page.locator('[data-executor-subtab="config"]').click()
            body = page.locator("#executor-config-body")
            row = body.locator(".executor-config-row").filter(
                has=page.locator('.executor-config-name:text-is("log_level")')
            )
            row.get_by_role("button", name="Edit").click()
            row.locator("select.executor-config-input").select_option("info")
            row.get_by_role("button", name="Set", exact=True).click()
            body.get_by_role("button", name="Save changes").click()
            expect(body).to_contain_text("Saved. Restart to apply.")
            body.get_by_role("button", name="Apply saved file").click()
            expect(body).to_contain_text(
                "CLI / ENV changes require restarting Parent"
            )
            body.get_by_role("button", name="Confirm restart").click()
            expect(body).to_contain_text("Configuration applied", timeout=40000)
            assert h.server.pid == control_pid
            assert (
                tmp_path / "control.yaml"
            ).read_text() == "log_level: info\n"

            page.locator('[data-executor-subtab="overview"]').click()
            h.wait_executor_online()
            page.locator(
                '#executor-list button:not([data-selection="__control__"])'
            ).first.click()
            page.locator('[data-executor-subtab="config"]').click()
            executor = page.locator("#executor-config-body")
            row = executor.locator(".executor-config-row").filter(
                has=page.locator('.executor-config-name:text-is("max_jobs")')
            )
            row.get_by_role("button", name="Edit").click()
            row.locator("input.executor-config-input").fill("32")
            row.get_by_role("button", name="Set", exact=True).click()
            executor.get_by_role("button", name="Save changes").click()
            expect(executor).to_contain_text("Saved. Restart to apply.")
            executor.get_by_role("button", name="Apply saved file").click()
            executor.get_by_role("button", name="Confirm restart").click()
            expect(executor).to_contain_text(
                "Configuration applied", timeout=40000
            )
            assert h.executor.pid == executor_pid
            assert '"max_jobs": 32' in (tmp_path / "executor.json").read_text()
            # A Control Child restart briefly refuses requests; those specific
            # network events are expected, but JavaScript errors are not.
            assert not h.page_errors, "\n".join(h.page_errors)
            assert not [
                message
                for message in h.console_errors
                if "ERR_CONNECTION_REFUSED" not in message
                and "503 (Service Unavailable)" not in message
                and "502 (Bad Gateway)" not in message
            ], "\n".join(h.console_errors)
            assert not [
                message
                for message in h.request_failures
                if "ERR_CONNECTION_REFUSED" not in message
                and "ERR_ABORTED" not in message
                and "favicon.ico" not in message
            ], "\n".join(h.request_failures)
            failed = False
        finally:
            h.stop(failed=failed)
