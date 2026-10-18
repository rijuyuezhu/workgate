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
    for owner, name, config in (
        (
            "control",
            "shared-mcp",
            {"type": "http", "url": "https://example.org/mcp"},
        ),
        ("executor", "shared-mcp", {"type": "stdio", "command": "echo"}),
        ("executor", "local-mcp", {"type": "stdio", "command": "echo"}),
    ):
        result = harness.api(
            "POST",
            "/api/ui/integrations",
            body={
                "owner": owner,
                "executor_id": harness.executor_id,
                "kind": "mcp",
                "action": "register",
                "name": name,
                "config": config,
            },
        )
        assert result["status"] == 200, result
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
    # Session remains focused on Audit. Integrations open a separate drilldown.
    assert page.locator("#session-skills").count() == 0
    button = page.locator("#session-integrations-open")
    expect(button).to_be_enabled()
    button.click()
    pane = page.locator("#session-integrations")
    expect(pane).to_be_visible()
    expect(page.locator(".session-execution-detail")).to_be_hidden()
    expect(page.locator(".session-audit")).to_be_hidden()
    skills = page.locator("#session-integrations-skills-list")
    expect(skills).to_contain_text("browser-project")
    expect(skills).to_contain_text("Project")
    expect(skills.locator(".executor-manage-pill").first).to_have_text(
        "Effective"
    )
    assert skills.locator("button").count() == 0
    assert str(harness.executor_workspace) not in pane.inner_text()

    added = root / "on-refresh"
    added.mkdir()
    (added / "SKILL.md").write_text(
        "---\ndescription: Added after first query\n---\n# Refresh\n",
        encoding="utf-8",
    )
    page.locator("#session-integrations-skills-refresh").click()
    expect(skills).to_contain_text("on-refresh")
    mcp_tab = page.locator('[data-session-integrations-tab="mcp"]')
    mcp_tab.click()
    expect(mcp_tab).to_have_attribute("aria-selected", "true")
    mcp_rows = page.locator(
        "#session-integrations-mcp-list .executor-manage-row"
    )
    expect(mcp_rows.filter(has_text="local-mcp")).to_be_visible()
    assert mcp_rows.filter(has_text="shared-mcp").count() == 2
    expect(mcp_rows.filter(has_text="shared-mcp").last).to_contain_text(
        "Overridden"
    )
    expect(
        page.locator("#session-integrations-mcp-status")
    ).not_to_contain_text("Loading")
    expect(page.locator("#session-integrations-skills")).to_be_hidden()
    page.locator('[data-session-integrations-tab="skills"]').click()
    expect(skills).to_contain_text("browser-project")

    page.set_viewport_size({"width": 390, "height": 844})
    expect(pane).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.screenshot(
        path=str(harness.artifacts / "session-integrations-mobile.png")
    )
    page.locator("#session-integrations-back").click()
    expect(pane).to_be_hidden()
    expect(page.locator(".session-audit")).to_be_visible()
    assert page.locator("#session-detail-executor").bounding_box() is not None

    # Management is delegated to the existing Executor editor, never copied.
    button.click()
    page.locator("#session-integrations-skills-manage").click()
    expect(page.locator("#page-title")).to_have_text("Executors")
    expect(page.locator('[data-executor-subtab="skills"]')).to_have_attribute(
        "aria-selected", "true"
    )
    harness.assert_clean_browser()
