"""Real Chromium: edit an Executor-managed multi-file Skill without arbitrary file browsing."""

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [pytest.mark.browser, pytest.mark.timeout(180)]


def test_executor_skill_auxiliary_files_chromium(
    browser_harness: BrowserHarness,
):
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    harness.navigate("executors")
    row = (
        page.locator("#executor-list .executor-row")
        .filter(has_text="online")
        .first
    )
    expect(row).to_be_visible()
    row.click()
    page.locator('[data-executor-subtab="skills"]').click()
    page.locator("#executor-skills-add").click()
    expect(page.locator("#executor-integration-dialog")).to_be_visible()
    expect(page.locator("#executor-integration-owner")).to_have_value(
        "executor"
    )
    expect(page.locator("#executor-skill-files-entry")).to_be_hidden()
    page.locator("#executor-integration-name").fill("browser-bundle")
    page.locator("#executor-integration-content").fill(
        "---\ndescription: Browser bundle\n---\n# Browser bundle\n"
    )
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).to_be_hidden()

    skill_row = page.locator(
        "#executor-skills-body .executor-manage-row"
    ).filter(has_text="browser-bundle")
    expect(skill_row).to_be_visible()
    skill_row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-dialog")).to_be_visible()
    expect(page.locator("#executor-skill-files-entry")).to_be_visible()
    opener = page.locator("#executor-skill-files-open")
    expect(opener).to_be_enabled()
    opener.click()
    modal = page.locator("#executor-skill-files-dialog")
    expect(modal).to_be_visible()
    expect(page.locator("#executor-skill-files-list")).to_have_count(1)

    page.locator("#executor-skill-files-path").fill("examples/checklist.md")
    page.locator("#executor-skill-files-content").fill("Check each step.\n")
    page.locator("#executor-skill-files-save").click()
    expect(page.locator("#executor-skill-files-list")).to_contain_text(
        "examples/checklist.md"
    )
    expect(page.locator("#executor-skill-files-save")).to_have_text(
        "Save changes"
    )

    # Browser fixture intentionally disables public agent tools; inspect the
    # real Executor-owned file through the authenticated management API.
    selected = harness.api(
        "GET",
        "/api/ui/integrations/skill-files?executor_id="
        + harness.executor_id
        + "&name=browser-bundle&action=get"
        + "&path=examples%2Fchecklist.md",
    )
    assert selected["status"] == 200, selected
    assert selected["payload"]["data"]["content"] == "Check each step.\n"

    page.locator("#executor-skill-files-content").fill("Check again.\n")
    page.locator("#executor-skill-files-save").click()
    expect(page.locator("#executor-skill-files-status")).to_have_text(
        "File saved."
    )
    page.screenshot(
        path=str(harness.artifacts / "skill-bundle-files-desktop.png")
    )
    # A cancelled close must preserve unsaved content, including its revision.
    page.locator("#executor-skill-files-content").fill("Unsaved draft.\n")
    page.once("dialog", lambda dlg: dlg.dismiss())
    page.locator("#executor-skill-files-close").click()
    expect(modal).to_be_visible()
    expect(page.locator("#executor-skill-files-content")).to_have_value(
        "Unsaved draft.\n"
    )
    page.once("dialog", lambda dlg: dlg.accept())
    page.locator("#executor-skill-files-reload").click()
    expect(page.locator("#executor-skill-files-content")).to_have_value(
        "Check again.\n"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    expect(modal).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(
        path=str(harness.artifacts / "skill-bundle-files-mobile.png")
    )

    page.once("dialog", lambda dlg: dlg.accept())
    page.locator("#executor-skill-files-delete").click()
    expect(page.locator("#executor-skill-files-status")).to_have_text(
        "File deleted."
    )
    expect(page.locator("#executor-skill-files-list")).not_to_contain_text(
        "examples/checklist.md"
    )
    page.locator("#executor-skill-files-close").click()
    expect(modal).to_be_hidden()
    expect(page.locator("#executor-integration-dialog")).to_be_visible()
    page.locator("#executor-integration-owner").select_option("control")
    expect(page.locator("#executor-skill-files-entry")).to_be_hidden()
    page.locator("#executor-integration-owner").select_option("executor")
    expect(page.locator("#executor-skill-files-entry")).to_be_visible()
    page.locator("#executor-integration-close").click()
    expect(page.locator("#executor-integration-dialog")).to_be_hidden()
    harness.assert_clean_browser()
