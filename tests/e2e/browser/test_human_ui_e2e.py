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
    expect(page.locator("#executor-integration-owner")).to_have_value(
        "executor"
    )
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
    expect(page.locator("#executor-mcp-auth-status")).to_be_hidden()
    page.locator("#executor-mcp-secret-details summary").click()
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
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "Secret removed"
    )
    expect(page.locator("#executor-integration-status")).to_have_attribute(
        "data-tone", "success"
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
        "Not connected"
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
    expect(row).to_have_css("background-color", "rgb(244, 251, 247)")
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
    expect(local).to_have_css("background-color", "rgb(244, 251, 247)")
    local.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).to_have_value("echo")
    # Editing must preserve disabled state; an unrelated edit cannot turn a
    # previously disabled MCP back on.
    page.locator("#executor-integration-toggle").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    expect(local).to_contain_text("Disabled")
    expect(local).to_have_css("background-color", "rgb(238, 240, 243)")
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


def test_control_skill_owner_in_existing_editor_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    """Control-wide SKILL.md CRUD uses the same Owner editor as Executor Skills."""
    browser_harness.login()
    browser_harness.wait_executor_online()
    page = browser_harness.page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{browser_harness.base_url}/ui#executors")
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="skills"]').click()
    page.locator("#executor-skills-add").click()
    owner = page.locator("#executor-integration-owner")
    expect(owner).to_be_enabled()
    owner.select_option("control")
    expect(page.locator("#executor-integration-mcp-fields")).to_be_hidden()
    page.locator("#executor-integration-name").fill("browser-control-skill")
    first = "---\ndescription: Shared Control helper\n---\n# Shared\n"
    page.locator("#executor-integration-content").fill(first)
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).to_be_hidden()
    control_group = page.locator(
        "#executor-skills-body .executor-manage-group"
    ).filter(has=page.get_by_role("heading", name="Control", exact=True))
    row = control_group.locator(".executor-manage-row").filter(
        has_text="browser-control-skill"
    )
    expect(row).to_be_visible()
    row.get_by_role("button", name="Manage").click()
    expect(owner).to_have_value("control")
    expect(owner).to_be_enabled()
    expect(page.locator("#executor-integration-content")).to_be_enabled()
    expect(page.locator("#executor-integration-content")).to_have_value(first)
    page.locator("#executor-integration-content").fill(first + "No copies.\n")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).to_be_hidden()
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-content")).to_be_enabled()
    expect(page.locator("#executor-integration-content")).to_have_value(
        first + "No copies.\n"
    )
    page.locator("#executor-integration-toggle").click()
    expect(row).to_contain_text("Disabled")
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-toggle")).to_be_enabled()
    page.locator("#executor-integration-toggle").click()
    expect(row).to_contain_text("Enabled")
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-content")).to_be_enabled()
    owner.select_option("executor")
    expect(page.locator("#executor-integration-delete")).to_be_hidden()
    expect(page.locator("#executor-integration-toggle")).to_be_hidden()
    page.evaluate("""() => {
      const fetchOriginal = globalThis.fetch.bind(globalThis);
      globalThis.fetch = (...args) => String(args[0]).includes('/integrations/skill-move')
        ? new Promise((resolve) => setTimeout(() => resolve(fetchOriginal(...args)), 750))
        : fetchOriginal(...args);
    }""")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-cancel")).to_be_disabled()
    expect(page.locator("#executor-integration-close")).to_be_disabled()
    expect(page.locator("#executor-integration-content")).not_to_be_editable()
    page.keyboard.press("Escape")
    expect(page.locator("#executor-integration-dialog")).to_be_visible()
    expect(page.locator("#executor-integration-dialog")).to_be_hidden()
    expect(control_group).not_to_contain_text("browser-control-skill")
    executor_group = page.locator(
        "#executor-skills-body .executor-manage-group"
    ).filter(has=page.get_by_role("heading", name="Executor", exact=True))
    executor_row = executor_group.locator(".executor-manage-row").filter(
        has_text="browser-control-skill"
    )
    expect(executor_row).to_be_visible()
    executor_row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-content")).to_be_enabled()
    expect(page.locator("#executor-integration-content")).to_have_value(
        first + "No copies.\n"
    )
    owner.select_option("control")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).to_be_hidden()
    expect(row).to_be_visible()
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-content")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(control_group).not_to_contain_text("browser-control-skill")
    browser_harness.assert_clean_browser()


def test_skill_editor_old_request_cannot_unlock_new_editor(
    browser_harness: BrowserHarness,
) -> None:
    """A cancelled async Validate cannot enable another Skill's unloaded controls."""
    browser_harness.login()
    browser_harness.wait_executor_online()
    page = browser_harness.page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{browser_harness.base_url}/ui#executors")
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="skills"]').click()

    for name in ("async-editor-first", "async-editor-second"):
        page.locator("#executor-skills-add").click()
        page.locator("#executor-integration-owner").select_option("control")
        page.locator("#executor-integration-name").fill(name)
        page.locator("#executor-integration-content").fill("# Test Skill\n")
        page.locator("#executor-integration-save").click()
        expect(page.locator("#executor-integration-dialog")).to_be_hidden()

    group = page.locator("#executor-skills-body .executor-manage-group").filter(
        has=page.get_by_role("heading", name="Control", exact=True)
    )
    first = group.locator(".executor-manage-row").filter(
        has_text="async-editor-first"
    )
    second = group.locator(".executor-manage-row").filter(
        has_text="async-editor-second"
    )
    first.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-content")).to_be_enabled()
    page.evaluate("""() => {
      const fetchOriginal = globalThis.fetch.bind(globalThis);
      globalThis.fetch = (...args) => {
        const url = String(args[0]);
        const body = String(args[1]?.body || '');
        const delay = body.includes('"action":"validate"') ? 900
          : url.includes('action=get') && url.includes('name=async-editor-second') ? 2400 : 0;
        return delay ? new Promise((resolve) => setTimeout(() => resolve(fetchOriginal(...args)), delay))
          : fetchOriginal(...args);
      };
    }""")
    page.locator("#executor-integration-validate").click()
    page.locator("#executor-integration-cancel").click()
    second.get_by_role("button", name="Manage").click()
    page.wait_for_timeout(1300)
    expect(page.locator("#executor-integration-save")).to_be_disabled()
    expect(page.locator("#executor-integration-delete")).to_be_disabled()
    expect(page.locator("#executor-integration-toggle")).to_be_disabled()
    expect(page.locator("#executor-integration-content")).to_be_disabled()
    expect(page.locator("#executor-integration-content")).to_be_enabled()
    page.locator("#executor-integration-cancel").click()

    for row in (first, second):
        row.get_by_role("button", name="Manage").click()
        expect(page.locator("#executor-integration-content")).to_be_enabled()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#executor-integration-delete").click()
        expect(row).to_have_count(0)
    browser_harness.assert_clean_browser()


def test_control_card_switching_config_refresh_and_mobile(
    browser_harness: BrowserHarness,
) -> None:
    """Control is a first-class UI selection, never a fake Executor request."""
    browser_harness.login()
    browser_harness.wait_executor_online()
    page = browser_harness.page
    urls: list[str] = []
    page.on("request", lambda request: urls.append(request.url))
    page.set_viewport_size({"width": 1360, "height": 900})
    page.goto(f"{browser_harness.base_url}/ui#executors")

    control = page.locator(
        '#executor-list button[data-selection="__control__"]'
    )
    executor = page.locator(
        '#executor-list button:not([data-selection="__control__"])'
    ).first
    expect(control).to_be_visible()
    expect(page.locator("#executor-list button").first).to_have_attribute(
        "data-selection", "__control__"
    )
    expect(page.locator("#executor-detail-name")).to_have_text("Control")
    expect(page.locator(".executor-detail-actions")).to_be_hidden()
    expect(page.locator("#executor-control-overview")).to_be_visible()
    expect(page.locator("#executor-detail-grid")).to_be_hidden()

    page.locator('[data-executor-subtab="mcp"]').click()
    expect(
        page.locator("#executor-mcp-body .executor-manage-group")
    ).to_have_count(1)
    expect(page.locator("#executor-mcp-body")).to_contain_text("Control")
    page.locator('[data-executor-subtab="skills"]').click()
    expect(
        page.locator("#executor-skills-body .executor-manage-group")
    ).to_have_count(1)
    expect(page.locator("#executor-skills-body")).to_contain_text("Control")

    page.locator('[data-executor-subtab="config"]').click()
    settings = page.locator("#executor-config-body")
    expect(settings).to_contain_text("oauth_admin_pin")
    first_field = settings.locator(".executor-config-row").first
    expect(first_field).to_be_visible()
    # Polling updates state without replacing the selected card or Config DOM.
    page.evaluate(
        """() => { window.__controlCard = document.querySelector('[data-selection="__control__"]'); }"""
    )
    page.wait_for_timeout(4300)
    assert page.evaluate(
        """() => window.__controlCard === document.querySelector('[data-selection="__control__"]')"""
    )
    expect(first_field).to_be_visible()
    expect(page.locator('[data-executor-subtab="config"]')).to_have_attribute(
        "aria-selected", "true"
    )

    expect(executor).to_be_visible()
    executor.click()
    expect(page.locator("#executor-detail-name")).not_to_have_text("Control")
    expect(page.locator("#executor-control-overview")).to_be_hidden()
    expect(page.locator("#executor-detail-grid")).to_be_visible()
    expect(page.locator(".executor-detail-actions")).to_be_visible()
    page.locator('[data-executor-subtab="config"]').click()
    expect(settings).to_contain_text("max_jobs")
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-add")).to_be_enabled()
    page.locator("#executor-mcp-add").click()
    expect(page.locator("#executor-integration-dialog")).to_be_visible()

    # Losing the selected Executor must close its uncommitted editor and
    # fall back to Control Config without a fake executor_id.
    def remove_executor(route):
        response = route.fetch()
        payload = response.json()
        payload["data"]["executors"] = []
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/executors", remove_executor)
    expect(page.locator("#executor-detail-name")).to_have_text(
        "Control", timeout=10000
    )
    expect(page.locator("#executor-integration-dialog")).to_be_hidden()
    page.locator('[data-executor-subtab="config"]').click()
    expect(settings).to_contain_text("oauth_admin_pin")
    page.unroute("**/api/ui/executors", remove_executor)

    # A failed inventory request must not leave stale management actions live.
    def unavailable(route):
        route.fulfill(
            status=200, json={"ok": False, "message": "Simulated outage"}
        )

    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-add")).to_be_enabled()
    page.route("**/api/ui/executors", unavailable)
    expect(page.locator("#executor-detail-status")).to_have_text(
        "Unavailable", timeout=10000
    )
    expect(page.locator("#executor-mcp-add")).to_be_disabled()
    expect(page.locator("#executor-mcp-body")).to_contain_text("Unavailable")
    page.unroute("**/api/ui/executors", unavailable)
    expect(page.locator("#executor-detail-status")).to_have_text(
        "Available", timeout=10000
    )
    expect(page.locator("#executor-mcp-add")).to_be_enabled(timeout=10000)

    # Real mobile list/detail navigation also applies to Control.
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator(".executor-details")).to_be_hidden()
    control.click()
    expect(page.locator(".executor-details")).to_be_visible()
    expect(page.locator("#executor-detail-name")).to_have_text("Control")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.locator(".mobile-subpage-bar:visible .mobile-subpage-back").click()
    expect(page.locator(".executor-details")).to_be_hidden()
    # The UI selection marker must never leak into executor-scoped APIs.
    assert not any("executor_id=__control__" in url for url in urls)
    browser_harness.assert_clean_browser()


def test_standalone_control_mcp_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    """Control MCP remains manageable with zero Executors in the UI inventory."""
    browser_harness.login()
    browser_harness.wait_executor_online()
    page = browser_harness.page

    def empty_inventory(route):
        response = route.fetch()
        payload = response.json()
        payload["data"]["executors"] = []
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/executors", empty_inventory)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{browser_harness.base_url}/ui#executors")
    control_card = page.locator(
        '#executor-list button[data-selection="__control__"]'
    )
    expect(control_card).to_be_visible()
    expect(page.locator("#executor-list button")).to_have_count(1)
    control_card.click()
    expect(page.locator(".executor-details")).to_be_visible()
    expect(page.locator("#executor-detail-name")).to_have_text("Control")
    expect(page.locator(".executor-detail-actions")).to_be_hidden()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator("#executor-mcp-add")).to_be_enabled()
    page.locator("#executor-mcp-add").click()
    expect(page.locator("#executor-integration-owner")).to_have_value("control")
    submitted = []
    page.on(
        "request",
        lambda req: (
            submitted.append(req.post_data_json)
            if req.method == "POST" and req.url.endswith("/api/ui/integrations")
            else None
        ),
    )
    page.locator("#executor-integration-name").fill("standalone-control")
    page.locator("#executor-integration-url").fill("https://example.org/mcp")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    assert submitted[-1]["owner"] == "control"
    assert submitted[-1].get("executor_id") is None
    row = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="standalone-control"
    )
    expect(row).to_be_visible()
    row.get_by_role("button", name="Manage").click()
    page.locator("#executor-integration-toggle").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    expect(row).to_contain_text("Disabled")
    row.get_by_role("button", name="Manage").click()
    page.locator("#executor-mcp-advanced-settings > summary").click()
    page.locator("#executor-integration-test").click()
    expect(page.locator("#executor-integration-test-result")).to_contain_text(
        "disabled"
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-mcp-body")).not_to_contain_text(
        "standalone-control"
    )
    page.locator('[data-executor-subtab="skills"]').click()
    expect(page.locator("#executor-skills-add")).to_be_enabled()
    page.locator("#executor-skills-add").click()
    expect(page.locator("#executor-integration-owner")).to_have_value("control")
    page.locator("#executor-integration-name").fill("standalone-skill")
    page.locator("#executor-integration-content").fill("# Control Skill\n")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    assert submitted[-1]["owner"] == "control"
    assert submitted[-1].get("executor_id") is None
    skill = page.locator("#executor-skills-body .executor-manage-row").filter(
        has_text="standalone-skill"
    )
    expect(skill).to_be_visible()
    skill.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-content")).to_be_enabled()
    expect(page.locator("#executor-integration-content")).to_have_value(
        "# Control Skill\n"
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(skill).to_have_count(0)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    page.unroute("**/api/ui/executors", empty_inventory)
    browser_harness.assert_clean_browser()


def test_mcp_location_apply_real_chromium(
    browser_harness: BrowserHarness,
) -> None:
    """Editing Owner + Apply migrates one MCP; no separate Move dialog."""
    browser_harness.login()
    browser_harness.wait_executor_online()
    page = browser_harness.page
    page.set_viewport_size({"width": 1320, "height": 900})
    page.goto(f"{browser_harness.base_url}/ui#executors")
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="mcp"]').click()
    page.locator("#executor-mcp-add").click()
    page.locator("#executor-integration-owner").select_option("executor")
    page.locator("#executor-integration-name").fill("browser-relocate")
    page.locator("#executor-integration-command").fill("echo")
    page.locator("#executor-integration-save").click()
    local = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="browser-relocate"
    )
    expect(local).to_be_visible()
    page.evaluate("""() => {
      const fetchOriginal = globalThis.fetch.bind(globalThis);
      globalThis.fetch = (...args) => String(args[0]).includes('action=get')
        && String(args[0]).includes('name=browser-relocate')
        ? new Promise((resolve) => setTimeout(() => resolve(fetchOriginal(...args)), 700))
        : fetchOriginal(...args);
    }""")
    local.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).not_to_be_editable()
    expect(page.locator("#executor-integration-owner")).to_be_disabled()
    expect(page.locator("#executor-integration-owner")).to_be_enabled()
    page.locator("#executor-integration-owner").select_option("control")
    expect(page.locator("#executor-integration-url")).to_be_visible()
    page.locator("#executor-integration-url").fill("https://example.org/mcp")
    page.evaluate("""() => {
      const fetchOriginal = globalThis.fetch.bind(globalThis);
      globalThis.fetch = (...args) => String(args[0]).includes('/integrations/move')
        ? new Promise((resolve) => setTimeout(() => resolve(fetchOriginal(...args)), 750))
        : fetchOriginal(...args);
    }""")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-url")).not_to_be_editable()
    expect(page.locator("#executor-integration-args")).not_to_be_editable()
    expect(page.locator("#executor-integration-auth-mode")).to_be_disabled()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    remote = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="browser-relocate"
    )
    expect(remote).to_be_visible()
    expect(
        page.locator("#executor-mcp-body .executor-manage-group").filter(
            has_text="Control"
        )
    ).to_contain_text("browser-relocate")
    remote.get_by_role("button", name="Manage").click()
    page.locator("#executor-integration-owner").select_option("executor")
    expect(page.locator("#executor-integration-command")).to_be_visible()
    page.locator("#executor-integration-command").fill("printf")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    local = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="browser-relocate"
    )
    expect(
        page.locator("#executor-mcp-body .executor-manage-group").filter(
            has_text="Executor"
        )
    ).to_contain_text("browser-relocate")
    local.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).to_have_value(
        "printf"
    )
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#executor-integration-delete").click()
    expect(page.locator("#executor-mcp-body")).not_to_contain_text(
        "browser-relocate"
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
