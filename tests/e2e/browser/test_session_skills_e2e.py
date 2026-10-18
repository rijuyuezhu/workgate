"""Real Chromium exercises explicit Session-bound Skill discovery."""

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [pytest.mark.browser, pytest.mark.timeout(180)]


def test_session_skills_read_only_and_mobile(browser_harness: BrowserHarness):
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    root = harness.executor_workspace / ".agents" / "skills"
    skill = root / "browser-project"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\ndescription: Browser project test\n---\n# Browser project\n",
        encoding="utf-8",
    )
    started = harness.api(
        "POST",
        "/tools/session_start",
        body={
            "workdir": str(harness.executor_workspace),
            "executor_id": harness.executor_id,
            "label": "project-skills-browser",
        },
    )
    assert started["status"] == 200, started
    sid = started["payload"]["structuredContent"]["session_id"]
    page = harness.page
    harness.navigate("tasks")
    page.locator("#tasks-refresh").click()
    # Implicit Task creation places the new Session in the normal Task list.
    entry = page.locator("#task-list .task-entry").filter(
        has_text="project-skills-browser"
    )
    expect(entry).to_be_visible()
    entry.click()
    page.locator("#task-tab-sessions").click()
    session = page.locator(
        f'#session-list .session-entry[data-session-id="{sid}"]'
    )
    expect(session).to_be_visible()
    session.click()
    details = page.locator("#session-skills")
    expect(details).to_be_visible()
    assert not details.evaluate("node => node.open")
    details.locator("summary").click()
    expect(page.locator("#session-skills-list")).to_contain_text(
        "browser-project"
    )
    expect(page.locator("#session-skills-list")).to_contain_text("Project")
    expect(
        page.locator("#session-skills-list .executor-manage-row").filter(
            has_text="browser-project"
        )
    ).to_be_visible()
    expect(
        page.locator("#session-skills-list .executor-row-icon").first
    ).to_be_visible()
    expect(
        page.locator("#session-skills-list .executor-manage-pill").first
    ).to_have_text("Effective")
    assert page.locator("#session-skills-list button").count() == 0
    assert (
        str(harness.executor_workspace)
        not in page.locator("#session-skills").inner_text()
    )

    new_skill = root / "on-refresh"
    new_skill.mkdir()
    (new_skill / "SKILL.md").write_text(
        "---\ndescription: Added after first query\n---\n# Refresh\n",
        encoding="utf-8",
    )
    page.locator("#session-skills-refresh").click()
    expect(page.locator("#session-skills-list")).to_contain_text("on-refresh")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#session-skills")).to_be_visible()
    executor_box = page.locator("#session-detail-executor").bounding_box()
    assert executor_box is not None and executor_box["height"] < 50
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(
        path=str(harness.artifacts / "session-project-skills-mobile.png")
    )
    harness.assert_clean_browser()
