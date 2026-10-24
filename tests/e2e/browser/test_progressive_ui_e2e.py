"""Real Chromium checks that slow Executor resources do not block Control UI."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(120),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_startup_renders_independent_views_while_tasks_never_answer(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    page = harness.context.new_page()
    try:
        page.add_init_script(
            """(() => {
              const original = window.fetch.bind(window);
              window.fetch = (url, options) => {
                if (String(url).includes('/api/ui/tasks?')) {
                  window.__slowTasksStarted = true;
                  return new Promise(() => {});
                }
                return original(url, options);
              };
            })();"""
        )
        page.goto(f"{harness.base_url}/ui#audit", wait_until="domcontentloaded")
        expect(page.locator("#connection-state")).to_have_text("Connected")
        expect(page.locator("#executor-target-total")).to_have_text("1")
        expect(page.locator("#audit-state")).to_contain_text(
            "records loaded", timeout=10000
        )
        assert page.evaluate("window.__slowTasksStarted === true")
        page.locator('.nav-item[data-view="executors"]').click()
        expect(
            page.locator("#executor-list .executor-row").first
        ).to_be_visible()
    finally:
        page.close()


def test_control_integrations_render_before_executor_source(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.context.new_page()
    try:
        page.add_init_script(
            """(() => {
              const original = window.fetch.bind(window);
              window.fetch = (url, options) => {
                const path = String(url);
                if (path.includes('/api/ui/integrations?')
                    && path.includes('owner=executor') && path.includes('kind=skill')) {
                  window.__slowExecutorSkillStarted = true;
                  window.__slowExecutorSkillCalls = (window.__slowExecutorSkillCalls || 0) + 1;
                  return new Promise((resolve, reject) => {
                    window.__releaseExecutorSkill = () => original(url, options).then(resolve, reject);
                  });
                }
                return original(url, options);
              };
            })();"""
        )
        page.goto(
            f"{harness.base_url}/ui#executors", wait_until="domcontentloaded"
        )
        expect(page.locator("#connection-state")).to_have_text("Connected")
        online = page.locator("#executor-list .executor-row").filter(
            has_text="online"
        )
        expect(online.first).to_be_visible(timeout=10000)
        online.first.click()
        page.locator('[data-executor-subtab="skills"]').click()
        control = page.locator(
            "#executor-skills-body .executor-manage-group"
        ).filter(has=page.locator("h4", has_text="Control"))
        expect(control).to_be_visible(timeout=10000)
        expect(control).not_to_contain_text("Loading", timeout=10000)
        assert page.evaluate("window.__slowExecutorSkillStarted === true")
        expect(
            page.locator("#executor-skills-body .executor-manage-group").filter(
                has=page.locator("h4", has_text="Executor")
            )
        ).to_contain_text("Loading")
        page.locator('[data-executor-subtab="mcp"]').click()
        page.locator('[data-executor-subtab="skills"]').click()
        assert page.evaluate("window.__slowExecutorSkillCalls") == 1
        page.evaluate("""() => {
          window.__controlSkillGroup = [...document.querySelectorAll(
            '#executor-skills-body .executor-manage-group'
          )].find(group => group.querySelector('h4')?.textContent.startsWith('Control'));
          window.__releaseExecutorSkill();
        }""")
        expect(
            page.locator("#executor-skills-body .executor-manage-group").filter(
                has=page.locator("h4", has_text="Executor")
            )
        ).not_to_contain_text("Loading", timeout=10000)
        assert page.evaluate("""() =>
          document.querySelector('#executor-skills-body').contains(window.__controlSkillGroup)
        """), "An unrelated slow Executor response replaced the Control DOM"
    finally:
        page.close()


def test_logout_rejects_delayed_protected_audit_response(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    page = harness.context.new_page()
    try:
        page.add_init_script(
            """(() => {
              const original = window.fetch.bind(window);
              window.fetch = async (url, options) => {
                const result = await original(url, options);
                if (String(url).includes('/api/ui/audit?')) {
                  window.__auditResponseHeld = true;
                  await new Promise(resolve => { window.__releaseAuditResponse = resolve; });
                }
                return result;
              };
            })();"""
        )
        page.goto(f"{harness.base_url}/ui#audit", wait_until="domcontentloaded")
        expect(page.locator("#connection-state")).to_have_text("Connected")
        page.wait_for_function(
            "window.__auditResponseHeld === true", timeout=10000
        )
        page.locator("#sign-out").click()
        expect(page.locator("#auth-panel")).to_be_visible()
        page.evaluate("window.__releaseAuditResponse()")
        # A late read begun by the previous identity cannot restore Audit rows.
        expect(page.locator("#audit-list .audit-entry")).to_have_count(0)
        expect(page.locator("#auth-panel")).to_be_visible()
    finally:
        page.close()


def test_inventory_error_keeps_stale_rows_without_allowing_writes(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    page.goto(f"{harness.base_url}/ui#executors", wait_until="domcontentloaded")
    expect(page.locator("#executor-state")).to_contain_text(
        "Updated", timeout=10000
    )
    row = (
        page.locator("#executor-list .executor-row")
        .filter(has_text="online")
        .first
    )
    expect(row).to_be_visible()
    row.click()
    expect(page.locator("#executor-rename-open")).to_be_enabled()

    def fail_inventory(route):
        route.abort()

    page.route("**/api/ui/executors", fail_inventory)
    expect(page.locator("#executor-state")).to_contain_text(
        "Stale", timeout=10000
    )
    expect(row).to_be_visible()
    expect(page.locator("#executor-rename-open")).to_be_disabled()
    page.unroute("**/api/ui/executors", fail_inventory)
    expect(page.locator("#executor-state")).to_contain_text(
        "Updated", timeout=10000
    )
    expect(page.locator("#executor-rename-open")).to_be_enabled()


def test_retry_failed_control_skill_without_reloading_fresh_mcp(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    page = harness.context.new_page()
    counts = {"skill": 0, "mcp": 0}

    def transient_failure(route):
        url = route.request.url
        if "owner=control" not in url:
            route.continue_()
            return
        if "kind=skill" in url:
            counts["skill"] += 1
            if counts["skill"] == 1:
                route.abort()
                return
        elif "kind=mcp" in url:
            counts["mcp"] += 1
        route.continue_()

    page.route("**/api/ui/integrations?*", transient_failure)
    try:
        page.goto(
            f"{harness.base_url}/ui#executors", wait_until="domcontentloaded"
        )
        expect(page.locator("#connection-state")).to_have_text("Connected")
        expect(
            page.locator("#executor-list .executor-control-group .executor-row")
        ).to_be_visible()
        page.locator(
            "#executor-list .executor-control-group .executor-row"
        ).click()
        page.locator('[data-executor-subtab="skills"]').click()
        control = page.locator(
            "#executor-skills-body .executor-manage-group"
        ).filter(has=page.locator("h4", has_text="Control"))
        expect(control).to_contain_text("Failed", timeout=10000)
        assert counts["skill"] == 1
        assert counts["mcp"] == 1

        page.locator('[data-executor-subtab="overview"]').click()
        page.locator('[data-executor-subtab="skills"]').click()
        expect(control).not_to_contain_text("Failed", timeout=10000)
        assert counts["skill"] == 2
        assert counts["mcp"] == 1
    finally:
        page.close()
