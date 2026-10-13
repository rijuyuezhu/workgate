"""Real-browser regression for background polling preserving active Audit inspection."""

import time

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness


def run_background_polling(harness: BrowserHarness) -> None:
    page = harness.page

    # Accelerate only the 30-second application timer for this scenario. The
    # real controllers, authenticated backend and all fetches remain unchanged.
    def speed_up_poll(route):
        response = route.fetch()
        source = response.text()
        assert source.count("  }, 30000);") == 1
        route.fulfill(
            response=response, body=source.replace("  }, 30000);", "  }, 800);")
        )

    page.route("**/ui/assets/web.js*", speed_up_poll)
    seen = {"global": 0, "session": 0}

    def on_request(request):
        if "/api/ui/audit?" not in request.url:
            return
        for scope in seen:
            if f"scope={scope}" in request.url:
                seen[scope] += 1

    page.on("request", on_request)
    page.goto(f"{harness.base_url}/ui#audit", wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")

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
        start = seen[scope]
        deadline = time.monotonic() + 10
        while seen[scope] < start + 2 and time.monotonic() < deadline:
            page.wait_for_timeout(120)
        assert seen[scope] >= start + 2, f"{scope} did not poll twice"
        assert page.evaluate(
            "selector => document.querySelector(selector).firstElementChild === window.__pollDetail",
            detail_selector,
        )
        expect(detail).to_have_attribute("data-audit-entry-id", identity)
        expect(detail).to_have_attribute("data-expanded-pane", "result")
        expect(result.get_by_role("button", name="Tree")).to_have_attribute(
            "aria-pressed", "true"
        )
        expect(request.get_by_role("button", name="Raw")).to_have_attribute(
            "aria-pressed", "true"
        )
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
            expect(records).to_have_count(before + 1, timeout=10_000)
            expect(detail).to_have_attribute("data-audit-entry-id", identity)
            page.unroute("**/api/ui/audit?*", newer_record)
        different = next(
            (
                record
                for record in records.all()
                if record.get_attribute("data-audit-id")
                not in (identity, "new-background-audit-entry")
            ),
            None,
        )
        if different is not None:
            different.click()
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
    check_two_cycles(
        "session", "#session-audit-detail-body", "#session-audit-list"
    )

    page.remove_listener("request", on_request)
    page.unroute("**/ui/assets/web.js*", speed_up_poll)
    page.goto(f"{harness.base_url}/ui#overview", wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
