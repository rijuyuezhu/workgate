"""Two real isolated Executors: owner transfer and conflict handling in Chromium."""

import os
import time

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.e2e.browser.harness import BrowserHarness


@pytest.mark.browser
@pytest.mark.timeout(180)
@pytest.mark.skipif(
    os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="real Chromium"
)
def test_cross_executor_skill_ui_and_collision(tmp_path):
    artifacts = tmp_path / "artifacts"
    with sync_playwright() as playwright:
        h = BrowserHarness.start(
            tmp_path, artifacts, playwright, with_remote_executor=True
        )
        failed = True
        try:
            h.login()
            h.wait_executor_online()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rs = h.api("GET", "/api/ui/executors")
                rows = rs["payload"]["data"]["executors"]
                if any(
                    x["executor_id"] == h.remote_executor_id and x["online"]
                    for x in rows
                ):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("remote executor never became online")
            page = h.page
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{h.base_url}/ui#executors")
            page.locator("#executor-list .executor-row").filter(
                has_text="browser-loopback"
            ).click()
            page.locator('[data-executor-subtab="skills"]').click()
            page.locator("#executor-skills-add").click()
            page.locator("#executor-integration-name").fill("cross-host-skill")
            body = "---\ndescription: Cross-host helper\n---\n# Hello\n"
            page.locator("#executor-integration-content").fill(body)
            page.locator("#executor-integration-save").click()
            expect(
                page.locator("#executor-integration-dialog")
            ).not_to_be_visible()
            collision = h.api(
                "POST",
                "/api/ui/integrations",
                body={
                    "owner": "executor",
                    "executor_id": h.remote_executor_id,
                    "kind": "skill",
                    "action": "create",
                    "name": "cross-host-skill",
                    "content": "# Existing remote\n",
                },
            )
            assert collision["status"] == 200, collision
            local = page.locator(
                "#executor-skills-body .executor-manage-row"
            ).filter(has_text="cross-host-skill")
            local.get_by_role("button", name="Manage").click()
            expect(
                page.locator("#executor-integration-content")
            ).to_be_enabled()
            page.locator("#executor-integration-owner").select_option(
                f"executor:{h.remote_executor_id}"
            )
            expect(page.locator("#executor-integration-delete")).to_be_hidden()
            page.locator("#executor-integration-save").click()
            expect(
                page.locator("#executor-integration-status")
            ).to_contain_text("target Skill already exists")
            expect(page.locator("#executor-integration-content")).to_have_value(
                body
            )
            expect(page.locator("#executor-integration-save")).to_be_enabled()
            assert page.locator("#executor-integration-dialog").evaluate(
                "(e) => e.open"
            )
            remote_row = h.api(
                "GET",
                f"/api/ui/integrations?owner=executor&executor_id={h.remote_executor_id}&kind=skill&action=get&name=cross-host-skill",
            )
            assert remote_row["status"] == 200
            rev = remote_row["payload"]["data"]["skill"]["revision"]
            gone = h.api(
                "POST",
                "/api/ui/integrations",
                body={
                    "owner": "executor",
                    "executor_id": h.remote_executor_id,
                    "kind": "skill",
                    "action": "delete",
                    "name": "cross-host-skill",
                    "expected_revision": rev,
                },
            )
            assert gone["status"] == 200, gone
            page.locator("#executor-integration-save").click()
            expect(
                page.locator("#executor-integration-dialog")
            ).not_to_be_visible()
            expect(page.locator("#executor-detail-name")).to_contain_text(
                "browser-remote"
            )
            expect(
                page.locator('[data-executor-subtab="skills"]')
            ).to_have_attribute("aria-selected", "true")
            expect(page.locator("#executor-skills-body")).to_contain_text(
                "cross-host-skill"
            )
            assert page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth + 1"
            )
            dest = h.api(
                "GET",
                f"/api/ui/integrations?owner=executor&executor_id={h.remote_executor_id}&kind=skill&action=get&name=cross-host-skill",
            )
            assert (
                dest["status"] == 200
                and dest["payload"]["data"]["skill"]["content"] == body
            )
            src = h.api(
                "GET",
                f"/api/ui/integrations?owner=executor&executor_id={h.executor_id}&kind=skill&action=get&name=cross-host-skill",
            )
            assert src["status"] == 409
            # Cross-host MCP movement uses the same Owner + Apply UI.
            page.set_viewport_size({"width": 1320, "height": 900})
            page.locator("#executor-list .executor-row").filter(
                has_text="browser-loopback"
            ).click()
            page.locator('[data-executor-subtab="mcp"]').click()
            control_mcp = h.api(
                "POST",
                "/api/ui/integrations",
                body={
                    "owner": "control",
                    "kind": "mcp",
                    "action": "register",
                    "name": "cross-host-mcp",
                    "config": {
                        "type": "http",
                        "url": "https://example.org/mcp",
                    },
                },
            )
            assert control_mcp["status"] == 200
            page.locator("#executor-mcp-add").click()
            page.locator("#executor-integration-name").fill("cross-host-mcp")
            page.locator("#executor-integration-command").fill("echo")
            page.locator("#executor-integration-save").click()
            expect(
                page.locator("#executor-integration-dialog")
            ).not_to_be_visible()
            control_group = page.locator(
                "#executor-mcp-body .executor-manage-group"
            ).filter(
                has=page.get_by_role("heading", name="Control", exact=True)
            )
            control_row = control_group.locator(".executor-manage-row").filter(
                has_text="cross-host-mcp"
            )
            expect(control_row).to_contain_text("Overridden")
            expect(control_row).to_have_css(
                "background-color", "rgb(255, 250, 240)"
            )
            expect(control_row.locator(".executor-row-warning")).to_have_css(
                "color", "rgb(167, 113, 32)"
            )
            target_mcp = h.api(
                "POST",
                "/api/ui/integrations",
                body={
                    "owner": "executor",
                    "executor_id": h.remote_executor_id,
                    "kind": "mcp",
                    "action": "register",
                    "name": "cross-host-mcp",
                    "config": {"type": "stdio", "command": "printf"},
                },
            )
            assert target_mcp["status"] == 200, target_mcp
            local_mcp = page.locator(
                "#executor-mcp-body .executor-manage-group"
            ).filter(
                has=page.get_by_role("heading", name="Executor", exact=True)
            )
            local_mcp = local_mcp.locator(".executor-manage-row").filter(
                has_text="cross-host-mcp"
            )
            local_mcp.get_by_role("button", name="Manage").click()
            expect(page.locator("#executor-integration-command")).to_have_value(
                "echo"
            )
            page.locator("#executor-integration-owner").select_option(
                f"executor:{h.remote_executor_id}"
            )
            page.locator("#executor-integration-save").click()
            expect(
                page.locator("#executor-integration-status")
            ).to_contain_text("target name already exists")
            expect(page.locator("#executor-integration-command")).to_have_value(
                "echo"
            )
            remote_mcp = h.api(
                "GET",
                f"/api/ui/integrations?owner=executor&executor_id={h.remote_executor_id}&kind=mcp&action=get&name=cross-host-mcp",
            )
            assert remote_mcp["status"] == 200
            deleted = h.api(
                "POST",
                "/api/ui/integrations",
                body={
                    "owner": "executor",
                    "executor_id": h.remote_executor_id,
                    "kind": "mcp",
                    "action": "remove",
                    "name": "cross-host-mcp",
                    "expected_revision": remote_mcp["payload"]["data"][
                        "server"
                    ]["revision"],
                },
            )
            assert deleted["status"] == 200
            page.locator("#executor-integration-save").click()
            expect(
                page.locator("#executor-integration-dialog")
            ).not_to_be_visible()
            expect(page.locator("#executor-detail-name")).to_contain_text(
                "browser-remote"
            )
            expect(page.locator("#executor-mcp-body")).to_contain_text(
                "cross-host-mcp"
            )
            after = h.api(
                "GET",
                f"/api/ui/integrations?owner=executor&executor_id={h.remote_executor_id}&kind=mcp&action=get&name=cross-host-mcp",
            )
            assert after["status"] == 200
            assert (
                after["payload"]["data"]["editable_config"]["command"] == "echo"
            )
            gone = h.api(
                "GET",
                f"/api/ui/integrations?owner=executor&executor_id={h.executor_id}&kind=mcp&action=get&name=cross-host-mcp",
            )
            assert gone["status"] == 409
            # The intentional collision responses produce Chromium's 409 resource logs.
            assert all(
                "status of 409 (Conflict)" in msg for msg in h.console_errors
            ), h.console_errors
            h.console_errors.clear()
            h.assert_clean_browser()
            failed = False
        finally:
            h.stop(failed=failed)


def test_mcp_old_auth_request_cannot_erase_new_editor_input(browser_harness):
    """A finished request from a closed editor must not touch the next editor."""
    h = browser_harness
    h.login()
    h.wait_executor_online()
    for name in ("auth-race-first", "auth-race-second"):
        created = h.api(
            "POST",
            "/api/ui/integrations",
            body={
                "owner": "executor",
                "executor_id": h.executor_id,
                "kind": "mcp",
                "action": "register",
                "name": name,
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
    group = page.locator("#executor-mcp-body .executor-manage-group").filter(
        has=page.get_by_role("heading", name="Executor", exact=True)
    )
    first = group.locator(".executor-manage-row").filter(
        has_text="auth-race-first"
    )
    second = group.locator(".executor-manage-row").filter(
        has_text="auth-race-second"
    )
    first.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).to_have_value("echo")
    page.locator("#executor-mcp-secret-details summary").click()
    page.evaluate("""() => {
      const original = globalThis.fetch.bind(globalThis);
      globalThis.fetch = (...args) => String(args[0]).includes('/integrations/auth')
        && args[1]?.method === 'POST'
        ? new Promise(resolve => setTimeout(() => resolve(original(...args)), 1000))
        : original(...args);
    }""")
    page.locator("#executor-mcp-secret-name").fill("api_token")
    page.locator("#executor-mcp-secret-value").fill("race-value-a")
    page.locator("#executor-mcp-secret-set").click()
    page.locator("#executor-integration-cancel").click()
    second.get_by_role("button", name="Manage").click()
    expect(page.locator("#executor-integration-command")).to_have_value("echo")
    expect(page.locator("#executor-mcp-secret-set")).to_be_enabled()
    expect(page.locator("#executor-integration-test")).to_be_enabled()
    page.locator("#executor-mcp-secret-details summary").click()
    page.locator("#executor-mcp-secret-value").fill("race-value-b")
    page.wait_for_timeout(1300)
    expect(page.locator("#executor-mcp-secret-value")).to_have_value(
        "race-value-b"
    )
    h.assert_clean_browser()


@pytest.mark.browser
@pytest.mark.timeout(180)
@pytest.mark.skipif(
    os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="real Chromium"
)
def test_executor_selection_is_stable_during_apply_and_old_action_errors(
    tmp_path,
):
    """A sidebar switch cannot detach Apply or replay old Executor errors."""
    with sync_playwright() as playwright:
        h = BrowserHarness.start(
            tmp_path,
            tmp_path / "artifacts",
            playwright,
            with_remote_executor=True,
        )
        failed = True
        try:
            h.login()
            h.wait_executor_online()
            assert h.remote_executor_id
            created = h.api(
                "POST",
                "/api/ui/integrations",
                body={
                    "owner": "executor",
                    "executor_id": h.executor_id,
                    "kind": "skill",
                    "action": "create",
                    "name": "nav-lock-skill",
                    "content": "# Navigation lock regression\n",
                },
            )
            assert created["status"] == 200
            page = h.page
            page.goto(f"{h.base_url}/ui#executors")
            local = page.locator("#executor-list .executor-row").filter(
                has_text="browser-loopback"
            )
            remote = page.locator("#executor-list .executor-row").filter(
                has_text="browser-remote"
            )
            expect(remote).to_contain_text("online", timeout=20000)
            local.click()
            page.locator('[data-executor-subtab="skills"]').click()
            row = page.locator(
                "#executor-skills-body .executor-manage-row"
            ).filter(has_text="nav-lock-skill")
            row.get_by_role("button", name="Manage").click()
            expect(
                page.locator("#executor-integration-content")
            ).to_be_enabled()
            page.locator("#executor-integration-owner").select_option(
                f"executor:{h.remote_executor_id}"
            )
            page.evaluate("""() => {
              const original = globalThis.fetch.bind(globalThis);
              globalThis.fetch = (...args) =>
                String(args[0]).includes('/integrations/skill-move')
                ? new Promise(resolve => setTimeout(() => resolve(original(...args)), 850))
                : original(...args);
            }""")
            page.locator("#executor-integration-save").click()
            expect(
                page.locator("#executor-integration-cancel")
            ).to_be_disabled()
            # Synthetic sidebar click bypasses the modal backdrop to exercise
            # the select() guard itself (real pointer clicks hit the dialog).
            remote.evaluate("(el) => el.click()")
            expect(page.locator("#executor-detail-name")).to_contain_text(
                "browser-loopback"
            )
            expect(page.locator("#executor-integration-dialog")).to_be_visible()
            expect(page.locator("#executor-integration-dialog")).to_be_hidden()
            expect(page.locator("#executor-detail-name")).to_contain_text(
                "browser-remote"
            )
            # Old action responses must not create an error for a new selection.
            local.click()
            page.evaluate("""() => {
              const original = globalThis.fetch.bind(globalThis);
              globalThis.fetch = (...args) =>
                String(args[0]).includes('/executors/drain')
                ? new Promise(resolve => setTimeout(() => resolve(
                    new Response(JSON.stringify({ok:false,error:'Synthetic',message:'OLD ERROR'}),
                      {status:409,headers:{'Content-Type':'application/json'}})), 850))
                : original(...args);
            }""")
            page.locator("#executor-drain").click()
            remote.click()
            expect(page.locator("#executor-action-status")).to_be_empty()
            page.wait_for_timeout(1050)
            expect(page.locator("#executor-action-status")).to_be_empty()
            assert all("409" in error for error in h.console_errors), (
                h.console_errors
            )
            h.console_errors.clear()
            h.assert_clean_browser()
            failed = False
        finally:
            h.stop(failed=failed)
