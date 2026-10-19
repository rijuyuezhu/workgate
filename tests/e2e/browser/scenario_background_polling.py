"""Real-browser regression for background polling preserving active Audit inspection."""

import time

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def run_background_polling(harness: BrowserHarness) -> None:
    original_page = harness.page
    page = harness.context.new_page()
    harness.page = page
    harness._attach_diagnostics()

    # Speed up only the app's polling interval in this isolated page, while
    # continuing to run the real controller and authenticated HTTP responses.
    page.add_init_script("""(() => {
      const original = window.setInterval.bind(window);
      window.setInterval = (callback, delay, ...args) => {
        if (delay === 30000) {
          window.__workgateTestPollInstalled = true;
          return original(callback, 800, ...args);
        }
        return original(callback, delay, ...args);
      };
    })();""")
    page.goto(f"{harness.base_url}/ui#audit", wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    assert page.evaluate("window.__workgateTestPollInstalled === true")

    def advance_poll(scope: str) -> None:
        with page.expect_response(
            lambda response: (
                "/api/ui/audit?" in response.url
                and f"scope={scope}" in response.url
            ),
            timeout=10_000,
        ):
            page.wait_for_timeout(850)
        page.wait_for_timeout(120)  # Let the resolved response update the DOM.

    def check_two_cycles(scope, detail_selector, list_selector):
        detail = page.locator(detail_selector)
        records = page.locator(f"{list_selector} .audit-entry")
        expect(records.first).to_be_visible()
        records.first.click()
        result = detail.locator(".audit-call-panel").nth(1)
        expect(result).to_be_visible()
        request = detail.locator(".audit-call-panel").nth(0)
        request.get_by_role("button", name="Raw").click()
        detail.get_by_role("button", name="Call result").click()
        expect(detail).to_have_attribute("data-expanded-pane", "result")
        result.get_by_role("button", name="Tree").click()
        tree = result.locator("details.audit-tree-branch")
        expect(tree.first).to_be_visible()
        if tree.count() > 1:
            tree.nth(1).locator("summary").click()
        body = result.locator(".audit-call-panel-body")
        body.evaluate("el => {el.style.maxHeight = '45px'; el.scrollTop = 30;}")
        body.focus()
        expect(body).to_be_focused()
        identity = page.evaluate(
            "selector => {window.__pollDetail = document.querySelector(selector).firstElementChild; return document.querySelector(selector).dataset.auditEntryId}",
            detail_selector,
        )
        advance_poll(scope)
        advance_poll(scope)
        assert page.evaluate(
            "selector => document.querySelector(selector).firstElementChild === window.__pollDetail",
            detail_selector,
        )
        expect(detail).to_have_attribute("data-audit-entry-id", identity)
        expect(detail).to_have_attribute("data-expanded-pane", "result")
        expect(result.get_by_role("button", name="Tree")).to_have_attribute(
            "aria-pressed", "true"
        )
        # Collapsed panel controls are still present, but not in the ARIA tree.
        expect(
            request.locator(".audit-value-toolbar button").nth(1)
        ).to_have_attribute("aria-pressed", "true")
        if tree.count() > 1:
            expect(tree.nth(1)).to_have_attribute("open", "")
        expect(body).to_be_focused()
        assert body.evaluate("el => el.scrollTop") > 0
        if scope == "global":
            before = records.count()

            def newer_record(route):
                response = route.fetch()
                payload = response.json()
                entries = payload["data"]["entries"]
                payload["data"]["entries"] = [
                    {
                        "id": "new-background-audit-entry",
                        "tool": "write_file",
                        "operation": "files",
                        "event": "call",
                        "ts": time.time(),
                        "status": "completed",
                    },
                    *entries,
                ]
                payload["data"]["total_matched"] += 1
                route.fulfill(response=response, json=payload)

            page.route("**/api/ui/audit?*", newer_record)
            advance_poll("global")
            expect(records).to_have_count(before + 1, timeout=10_000)
            expect(detail).to_have_attribute("data-audit-entry-id", identity)
            page.unroute("**/api/ui/audit?*", newer_record)
        different_id = next(
            (
                record_id
                for record in records.all()
                if (record_id := record.get_attribute("data-audit-id"))
                not in (identity, "new-background-audit-entry")
            ),
            None,
        )
        if different_id is not None:
            # A background update can prepend/remove rows while Playwright is
            # waiting to click. Follow the record identity, not its index.
            page.locator(
                f'{list_selector} .audit-entry[data-audit-id="{different_id}"]'
            ).click()
            expect(detail).not_to_have_attribute(
                "data-audit-entry-id", identity
            )
            expect(detail.locator(".audit-call-panel")).to_have_count(2)

    page.locator("#audit-time").select_option("0")
    page.locator("#audit-operation").select_option("files")
    page.locator("#audit-search").fill("write_file")
    page.locator("#audit-refresh").click()
    check_two_cycles("global", "#audit-detail-body", "#audit-list")

    harness.navigate("tasks")
    page.locator("#task-list .task-entry").first.click()
    page.locator("#task-tab-sessions").click()
    expect(page.locator("#session-list .session-entry").first).to_be_visible()
    page.locator("#session-list .session-entry").first.click()
    page.locator("#session-audit-operation").select_option("files")
    page.locator("#session-audit-search").fill("write_file")
    page.locator("#session-audit-refresh").click()
    selected_task = page.locator(
        '#task-list .task-entry[aria-current="true"]'
    ).get_attribute("data-task-id")
    selected_session = page.locator(
        '#session-list .session-entry[aria-current="true"]'
    ).get_attribute("data-session-id")
    check_two_cycles(
        "session", "#session-audit-detail-body", "#session-audit-list"
    )
    expect(
        page.locator(f'#task-list .task-entry[data-task-id="{selected_task}"]')
    ).to_have_attribute("aria-current", "true")
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{selected_session}"]'
        )
    ).to_have_attribute("aria-current", "true")

    # Inventory polls and the 30s app timer must not discard an editor draft.
    harness.navigate("files")
    expect(page.locator('.file-entry[title="notes.txt"]')).to_be_visible()
    page.locator('.file-entry[title="notes.txt"]').click()
    page.locator("#file-edit").click()
    expect(page.locator("#file-editor-form")).to_be_visible()
    page.locator("#file-editor").fill("unsaved draft through background polls")
    file_executor = page.locator("#file-executor").input_value()
    terminal_executor = page.locator("#terminal-executor").input_value()
    advance_poll("global")
    advance_poll("global")
    expect(page.locator("#file-editor")).to_have_value(
        "unsaved draft through background polls"
    )
    expect(page.locator("#file-editor-form")).to_be_visible()
    expect(page.locator("#file-executor")).to_have_value(file_executor)
    expect(page.locator("#terminal-executor")).to_have_value(terminal_executor)

    # A genuinely removed session must clear its old Audit, not preserve it.
    harness.navigate("tasks")

    def removed_session(route):
        response = route.fetch()
        payload = response.json()
        task = payload["data"].get("task")
        if task and task["task_id"] == selected_task:
            task["sessions"] = [
                session
                for session in task["sessions"]
                if session["session_id"] != selected_session
            ]
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/tasks/detail?**", removed_session)
    with page.expect_response("**/api/ui/tasks/detail?**"):
        page.wait_for_timeout(850)
    expect(page.locator("#session-detail-title")).to_have_text(
        "No session selected"
    )
    expect(page.locator("#session-audit-list .audit-entry")).to_have_count(0)
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel")
    ).to_have_count(0)
    page.unroute("**/api/ui/tasks/detail?**", removed_session)

    page.close()
    harness.page = original_page
