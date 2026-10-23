"""Browser-driven feedback states: errors are not confused with routine status."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(240),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def assert_notice(page, selector: str, tone: str, symbol: str):
    target = page.locator(selector)
    expect(target).to_have_attribute("data-tone", tone)
    expect(target).to_have_class(
        __import__("re").compile(r"\bexecutor-notice\b")
    )
    assert (
        target.evaluate("(el) => getComputedStyle(el).borderStyle") == "solid"
    )
    assert (
        target.evaluate("(el) => getComputedStyle(el).borderTopWidth") == "1px"
    )
    assert (
        target.evaluate("(el) => getComputedStyle(el, '::before').content")
        == f'"{symbol}"'
    )
    assert (
        target.evaluate("(el) => getComputedStyle(el).backgroundColor")
        != "rgba(0, 0, 0, 0)"
    )


def test_executor_notice_states_and_mobile(browser_harness: BrowserHarness):
    h = browser_harness
    h.login()
    h.wait_executor_online()
    page = h.page
    page.goto(f"{h.base_url}/ui#executors")
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="skills"]').click()
    page.locator("#executor-skills-add").click()
    page.locator("#executor-integration-name").fill("notice-skill")
    page.locator("#executor-integration-content").fill(
        "---\ndescription: [\n---\n# Broken"
    )
    page.locator("#executor-integration-validate").click()
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "Invalid"
    )
    assert_notice(page, "#executor-integration-status", "error", "×")
    page.locator("#executor-integration-content").fill(
        "---\ndescription: Good\n---\n# Fine\n"
    )
    page.locator("#executor-integration-validate").click()
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "valid"
    )
    assert_notice(page, "#executor-integration-status", "success", "✓")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()

    row = page.locator("#executor-skills-body .executor-manage-row").filter(
        has_text="notice-skill"
    )
    row.get_by_role("button", name="Manage").click()
    page.locator("#executor-integration-owner").select_option("control")
    assert_notice(page, "#executor-integration-status", "warning", "!")
    page.locator("#executor-integration-owner").select_option("executor")
    expect(page.locator("#executor-integration-status")).to_be_empty()
    page.locator("#executor-integration-owner").select_option("control")
    assert_notice(page, "#executor-integration-status", "warning", "!")
    # Preserve the editor after a stale concurrent source update.
    current = h.api(
        "GET",
        f"/api/ui/integrations?kind=skill&owner=executor&executor_id={h.executor_id}&action=get&name=notice-skill",
    )
    assert current["status"] == 200
    updated = h.api(
        "POST",
        "/api/ui/integrations",
        body={
            "owner": "executor",
            "executor_id": h.executor_id,
            "kind": "skill",
            "action": "update",
            "name": "notice-skill",
            "content": "---\ndescription: Fresh\n---\n# Changed\n",
            "expected_revision": current["payload"]["data"]["skill"][
                "revision"
            ],
        },
    )
    assert updated["status"] == 200
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "changed"
    )
    assert_notice(page, "#executor-integration-status", "error", "×")
    expect(page.locator("#executor-integration-dialog")).to_be_visible()
    expect(page.locator("#executor-integration-save")).to_be_enabled()
    page.locator("#executor-integration-cancel").click()

    page.locator('[data-executor-subtab="mcp"]').click()
    page.locator("#executor-mcp-add").click()
    page.locator("#executor-integration-name").fill("notice-mcp")
    page.locator("#executor-integration-command").fill("echo")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).not_to_be_visible()
    mcp = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="notice-mcp"
    )
    mcp.get_by_role("button", name="Manage").click()
    page.locator("#executor-mcp-secret-details summary").click()
    page.locator("#executor-mcp-secret-set").click()
    assert_notice(page, "#executor-integration-status", "warning", "!")
    page.locator("#executor-mcp-secret-name").fill("api_key")
    page.locator("#executor-mcp-secret-value").fill("NOTICE_TEST_PRIVATE")
    page.locator("#executor-mcp-secret-set").click()
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "stored"
    )
    assert_notice(page, "#executor-integration-status", "success", "✓")
    page.locator("#executor-integration-test").click()
    expect(page.locator("#executor-integration-test-result")).to_contain_text(
        "MCP unavailable"
    )
    assert_notice(page, "#executor-integration-test-result", "warning", "!")
    page.locator("#executor-integration-cancel").click()

    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="mcp"]').click()
    page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="notice-mcp"
    ).get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-dialog")).to_be_visible()
    page.locator("#executor-integration-owner").select_option("control")
    assert_notice(page, "#executor-integration-status", "warning", "!")
    page.locator("#executor-integration-owner").select_option("executor")
    expect(page.locator("#executor-integration-status")).to_be_empty()
    page.locator("#executor-integration-owner").select_option("control")
    assert_notice(page, "#executor-integration-status", "warning", "!")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    assert page.locator("#executor-integration-status").evaluate(
        "(el) => el.scrollWidth <= el.clientWidth + 1"
    )
    page.screenshot(
        path=str(h.artifacts / "executor-warning-mobile.png"), full_page=True
    )
    page.set_viewport_size({"width": 320, "height": 700})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth + 1"
    )
    assert page.locator("#executor-integration-status").evaluate(
        "(el) => el.scrollWidth <= el.clientWidth + 1"
    )
    page.locator("#executor-integration-cancel").click()
    # Pairing controls are on the mobile list page; use desktop for this branch.
    page.set_viewport_size({"width": 1320, "height": 900})
    page.locator("#executor-pair-open").click()
    page.locator("#executor-pair-code").fill("UNKNOWN-CODE")
    page.locator("#executor-pair-form button[type=submit]").click()
    assert_notice(page, "#executor-pair-status", "error", "×")
    page.wait_for_timeout(4300)
    assert_notice(page, "#executor-pair-status", "error", "×")
    page.locator(
        "#executor-pair-dialog [data-close-executor-dialog]"
    ).first.click()
    # Deliberately rejected requests report 409/400 in Chromium's resource log.
    assert all(
        "status of 409 (Conflict)" in message
        or "status of 400 (Bad Request)" in message
        or "status of 404 (Not Found)" in message
        for message in h.console_errors
    ), h.console_errors
    h.console_errors.clear()
    h.assert_clean_browser()


def test_executor_offline_and_reconnect_notice_matrix(
    browser_harness: BrowserHarness,
):
    """Online→offline→online refresh keeps Control management and honest warnings."""
    h = browser_harness
    h.login()
    h.wait_executor_online()
    page = h.page
    page.goto(f"{h.base_url}/ui#executors")
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).first.click()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(
        page.locator("#executor-mcp-body .executor-manage-group").first
    ).to_be_visible()
    control_group = page.locator(
        "#executor-mcp-body .executor-manage-group"
    ).filter(has=page.locator("h4", has_text="Control"))
    expect(control_group).not_to_contain_text("Loading")
    page.evaluate("""() => {
      window.__controlMcpBeforeDisconnect = [...document.querySelectorAll(
        '#executor-mcp-body .executor-manage-group'
      )].find(group => group.querySelector('h4')?.textContent.startsWith('Control'));
    }""")
    h.stop_executor()
    expect(page.locator("#executor-detail-status")).to_have_text(
        "offline", timeout=85000
    )
    expect(
        page.locator("#executor-mcp-body .executor-manage-group").first
    ).to_contain_text("Offline")
    assert_notice(
        page,
        "#executor-mcp-body .executor-manage-group .executor-notice",
        "warning",
        "!",
    )
    expect(
        page.locator("#executor-mcp-add")
    ).to_be_enabled()  # Control can still manage MCP.
    assert page.evaluate("""() => document.querySelector(
      '#executor-mcp-body'
    ).contains(window.__controlMcpBeforeDisconnect)"""), (
        "Disconnecting Executor replaced unrelated Control MCP data"
    )
    page.locator('[data-executor-subtab="skills"]').click()
    expect(
        page.locator("#executor-skills-body .executor-manage-group").first
    ).to_contain_text("Offline")
    assert_notice(
        page,
        "#executor-skills-body .executor-manage-group:first-child .executor-notice",
        "warning",
        "!",
    )
    expect(
        page.locator("#executor-skills-add")
    ).to_be_enabled()  # Control still writable.
    h.restart_executor()
    h.wait_executor_online()
    expect(page.locator("#executor-detail-status")).to_have_text(
        "online", timeout=40000
    )
    expect(
        page.locator("#executor-skills-body .executor-manage-group").first
    ).not_to_contain_text("Offline", timeout=20000)
    expect(page.locator("#executor-skills-body")).to_contain_text("Control")
    h.assert_clean_browser()


def test_editor_operation_lock_and_partial_move_guidance(
    browser_harness: BrowserHarness,
):
    """MCP Test/Auth/Apply never overlap; partial moves require explicit recovery."""
    h = browser_harness
    h.login()
    h.wait_executor_online()
    created = h.api(
        "POST",
        "/api/ui/integrations",
        body={
            "owner": "executor",
            "executor_id": h.executor_id,
            "kind": "mcp",
            "action": "register",
            "name": "locked-mcp",
            "config": {"type": "stdio", "command": "echo"},
        },
    )
    assert created["status"] == 200, created
    page = h.page
    page.goto(f"{h.base_url}/ui#executors")
    page.locator("#executor-list .executor-row").filter(
        has_text="online"
    ).click()
    page.locator('[data-executor-subtab="mcp"]').click()
    row = page.locator("#executor-mcp-body .executor-manage-row").filter(
        has_text="locked-mcp"
    )
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).to_have_value("echo")
    owner = page.locator("#executor-integration-owner")
    owner.select_option("control")
    expect(page.locator("#executor-integration-toggle")).to_be_hidden()
    owner.select_option("executor")
    expect(page.locator("#executor-integration-toggle")).to_be_visible()
    page.locator("#executor-mcp-secret-details summary").click()
    page.evaluate("""() => {
      const original = globalThis.fetch.bind(globalThis);
      globalThis.fetch = (...args) => {
        const path = String(args[0]);
        const method = String(args[1]?.method || 'GET').toUpperCase();
        return (path.includes('/integrations/test') ||
          (path.includes('/integrations/auth') && method === 'POST'))
          ? new Promise(resolve => setTimeout(() => resolve(original(...args)), 900))
          : original(...args);
      };
    }""")
    page.locator("#executor-integration-test").click()
    for selector in [
        "#executor-integration-save",
        "#executor-integration-toggle",
        "#executor-integration-delete",
        "#executor-mcp-secret-set",
        "#executor-integration-owner",
    ]:
        expect(page.locator(selector)).to_be_disabled()
    expect(page.locator("#executor-integration-test-result")).to_contain_text(
        "MCP unavailable", timeout=20000
    )
    expect(page.locator("#executor-integration-save")).to_be_enabled()
    page.locator("#executor-mcp-secret-name").fill("test_key")
    page.locator("#executor-mcp-secret-value").fill("test-value-private")
    page.locator("#executor-mcp-secret-set").click()
    for selector in [
        "#executor-integration-save",
        "#executor-integration-test",
        "#executor-mcp-secret-set",
        "#executor-integration-delete",
    ]:
        expect(page.locator(selector)).to_be_disabled()
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "stored", timeout=15000
    )
    expect(page.locator("#executor-integration-save")).to_be_enabled()
    # The custom display:block on a colored notice cannot override [hidden].
    auth = page.locator("#executor-mcp-auth-status")
    auth.evaluate("""el => {
      el.classList.add('executor-notice'); el.textContent = 'should be hidden';
      el.hidden = true;
    }""")
    expect(auth).to_be_hidden()
    result_notice = page.locator("#executor-integration-test-result")
    result_notice.evaluate("""el => {
      el.hidden = true; el.classList.add('executor-notice');
    }""")
    expect(
        result_notice
    ).to_be_hidden()  # Also beats ID-specific display:block.

    # The API has a structured partial-move code, not just a message string.
    outcome = {"error": "MoveNeedsEnable"}

    def mock_move(route):
        route.fulfill(
            status=503,
            content_type="application/json",
            body=__import__("json").dumps(
                {
                    "ok": False,
                    "error": outcome["error"],
                    "message": "Move needs manual inspection or enablement",
                }
            ),
        )

    page.route("**/api/ui/integrations/move", mock_move)
    owner.select_option("control")
    page.locator("#executor-integration-url").fill("https://example.org/mcp")
    page.locator("#executor-integration-save").click()
    expect(page.locator("#executor-integration-dialog")).to_be_hidden()
    assert_notice(page, "#executor-action-status", "warning", "!")
    expect(page.locator("#executor-action-status")).to_contain_text(
        "remains disabled"
    )
    # Unconfirmed source removal is different: block retries, retain Cancel.
    row.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).to_have_value("echo")
    owner.select_option("control")
    page.locator("#executor-integration-url").fill("https://example.org/mcp")
    outcome["error"] = "MoveUncertain"
    page.locator("#executor-integration-save").click()
    assert_notice(page, "#executor-integration-status", "error", "×")
    expect(page.locator("#executor-integration-status")).to_contain_text(
        "Inspect both locations"
    )
    expect(page.locator("#executor-integration-save")).to_be_disabled()
    expect(page.locator("#executor-integration-url")).not_to_be_editable()
    expect(page.locator("#executor-integration-cancel")).to_be_enabled()
    page.evaluate("""() => {
      globalThis.__postMoveReads = 0;
      const previous = globalThis.fetch.bind(globalThis);
      globalThis.fetch = (...args) => {
        if (String(args[0]).includes('/integrations?')) globalThis.__postMoveReads++;
        return previous(...args);
      };
    }""")
    page.locator("#executor-integration-cancel").click()
    page.wait_for_function("globalThis.__postMoveReads > 0")
    assert all("503" in error for error in h.console_errors), h.console_errors
    h.console_errors.clear()
    h.assert_clean_browser()
