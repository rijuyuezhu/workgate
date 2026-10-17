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
