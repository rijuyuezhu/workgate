"""Real Chromium contracts for the shared React global/session Audit lists."""

import os
import time
from urllib.parse import parse_qs, urlparse

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


def test_react_audit_lists_navigation_and_stable_dom(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    page = harness.page
    assert page.evaluate(
        "typeof window.WorkgateReactAuditLists?.mount === 'function'"
    )

    # Keep real authenticated Audit controller / navigation, but add deterministic
    # entries so empty development databases do not affect this UI contract.
    now = time.time()
    entries = [
        {
            "id": "react-audit-a",
            "tool": "write_file",
            "operation": "files",
            "event": "call",
            "status": "completed",
            "ts": now,
            "duration_ms": 43,
            "session": "session-one",
        },
        {
            "id": "react-audit-b",
            "tool": "shell_exec",
            "operation": "shell",
            "event": "call",
            "ok": False,
            "ts": now - 20,
            "session": "session-two",
        },
    ]

    def with_records(route):
        if parse_qs(urlparse(route.request.url).query).get("scope") != [
            "global"
        ]:
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["data"]["entries"] = [dict(item) for item in entries]
        payload["data"]["total_matched"] = len(entries)
        route.fulfill(response=response, json=payload)

    def with_detail(route):
        params = parse_qs(urlparse(route.request.url).query)
        record = next(
            (
                item
                for item in entries
                if item["id"] == params.get("id", [""])[0]
            ),
            None,
        )
        if record is None:
            route.continue_()
            return
        route.fulfill(
            status=200,
            json={
                "ok": True,
                "data": {
                    "entry": {
                        **record,
                        "input": {"path": "sample.txt"},
                        "output": {"ok": True},
                    }
                },
            },
        )

    page.route("**/api/ui/audit?*", with_records)
    page.route("**/api/ui/audit/detail?*", with_detail)
    harness.navigate("audit")
    records = page.locator("#audit-list .audit-entry")
    expect(records).to_have_count(2)
    expect(records.first).to_have_attribute("data-audit-id", "react-audit-a")
    expect(records.first.locator(".audit-entry-title")).to_have_text(
        "write_file"
    )
    expect(records.first.locator(".audit-entry-meta")).to_contain_text(
        "completed"
    )
    expect(records.first.locator(".audit-entry-meta")).to_contain_text("43 ms")
    expect(records.nth(1).locator(".audit-entry-meta")).to_have_class(
        "audit-entry-meta audit-entry-status-failed"
    )
    expect(page.locator("#audit-previous")).to_be_disabled()
    expect(page.locator("#audit-next")).to_be_enabled()
    page.locator("#audit-next").click()
    expect(records.nth(1)).to_have_attribute("aria-current", "true")
    expect(page.locator("#audit-detail-title")).to_have_text("shell_exec")
    page.locator("#audit-previous").click()
    expect(records.first).to_have_attribute("aria-current", "true")
    records.first.focus()
    expect(records.first).to_be_focused()
    page.locator("#audit-list").press("ArrowDown")
    expect(records.nth(1)).to_have_attribute("aria-current", "true")
    page.locator("#audit-list").press("ArrowUp")
    expect(records.first).to_have_attribute("aria-current", "true")

    # React reconciliation must preserve buttons and list scroll on equal
    # authenticated refreshes, with no unnecessary attribute/child mutation.
    records.first.focus()
    page.evaluate("""() => {
      window.__firstAuditRow = document.querySelector('#audit-list .audit-entry');
      window.__auditChanges = 0;
      new MutationObserver(records => {
        for (const record of records) window.__auditChanges +=
          record.type === 'attributes' ? 1 : record.addedNodes.length + record.removedNodes.length;
      }).observe(document.getElementById('audit-list'), {
        subtree: true, attributes: true, childList: true
      });
    }""")
    with page.expect_response(
        lambda response: (
            "/api/ui/audit?" in response.url and "scope=global" in response.url
        )
    ):
        page.evaluate("() => document.getElementById('audit-refresh').click()")
    expect(page.locator("#audit-state")).to_contain_text("records loaded")
    page.wait_for_timeout(100)
    assert page.evaluate("window.__auditChanges") == 0
    assert records.first.evaluate("el => el === window.__firstAuditRow")
    expect(records.first).to_be_focused()

    # The same React component mounts independently for session Audit: moving
    # from global to session/reset must never reuse another scope's rows.
    page.evaluate("""() => {
      for (const scope of ['g', 's']) {
        const host = document.createElement('nav');
        host.className = 'audit-list';
        host.id = `audit-${scope}-contract`;
        document.getElementById('app-main').append(host);
        window[`__auditActions_${scope}`] = [];
        window[`__auditView_${scope}`] = window.WorkgateReactAuditLists.mount(
          host, id => window[`__auditActions_${scope}`].push(id)
        );
      }
      window.__syntheticRecords = [
        { id: 'one', tool: 'read_file', event: 'call', operation: 'files', ts: 123456, status: 'success' },
        { id: 'two', event: 'task_started', operation: 'agent', ts: 123457, status: 'unexpected' },
      ];
      window.__auditView_g.render(window.__syntheticRecords, 'one', 'No records');
      window.__auditView_s.render(window.__syntheticRecords, 'two', 'No session records');
    }""")
    global_list = page.locator("#audit-g-contract")
    session_list = page.locator("#audit-s-contract")
    expect(global_list.locator(".audit-entry")).to_have_count(2)
    expect(session_list.locator(".audit-entry")).to_have_count(2)
    expect(global_list.locator('[data-audit-id="one"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(session_list.locator('[data-audit-id="two"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(
        session_list.locator('[data-audit-id="two"] .audit-entry-title')
    ).to_have_text("task_started")
    expect(
        session_list.locator('[data-audit-id="two"] .audit-entry-meta')
    ).to_have_class("audit-entry-meta audit-entry-status-recorded")
    session_list.locator('[data-audit-id="one"]').click()
    assert page.evaluate("window.__auditActions_s") == ["one"]
    assert page.evaluate("window.__auditActions_g") == []
    session_row = session_list.locator('[data-audit-id="one"]')
    session_row.focus()
    session_list.evaluate(
        "el => { el.style.maxHeight = '24px'; el.scrollTop = 15; }"
    )
    scroll_before = session_list.evaluate("el => el.scrollTop")
    assert scroll_before > 0
    page.evaluate("""() => {
      window.__sessionRow = document.activeElement;
      window.__sessionChanges = 0;
      new MutationObserver(records => {
        for (const record of records) window.__sessionChanges +=
          record.type === 'attributes' ? 1 : record.addedNodes.length + record.removedNodes.length;
      }).observe(document.getElementById('audit-s-contract'), {
        subtree:true, attributes:true, childList:true
      });
      window.__auditView_s.render([...window.__syntheticRecords], 'two', 'No session records');
    }""")
    page.wait_for_timeout(100)
    assert page.evaluate("window.__sessionChanges") == 0
    assert session_list.evaluate("el => el.scrollTop") == scroll_before
    expect(session_row).to_be_focused()
    assert session_row.evaluate("el => el === window.__sessionRow")
    page.evaluate(
        """() => window.__auditView_s.render([], '', 'Session access revoked')"""
    )
    expect(session_list.locator(".audit-entry")).to_have_count(0)
    expect(session_list.locator(".empty-state")).to_have_text(
        "Session access revoked"
    )
    expect(global_list.locator(".audit-entry")).to_have_count(2)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
