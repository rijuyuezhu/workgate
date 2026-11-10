"""Authenticated and synthetic Chromium regression for shared React Audit details."""

import os
import time
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(140),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_react_audit_details_state_and_scopes(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    page = harness.page
    assert page.evaluate(
        "typeof window.WorkgateReactAuditDetails?.mount === 'function'"
    )

    # Exercise the actual authenticated Audit controller and HTTP selection.
    now = time.time()
    record = {
        "id": "react-detail-real",
        "tool": "write_file",
        "operation": "files",
        "event": "call",
        "status": "completed",
        "ts": now,
        "input": {"path": "safe.txt", "content": "sample"},
        "output": {"ok": True, "data": {"message": "write completed"}},
    }

    def records_route(route):
        if parse_qs(urlparse(route.request.url).query).get("scope") != [
            "global"
        ]:
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["data"]["entries"] = [record]
        payload["data"]["total_matched"] = 1
        payload["data"]["entry"] = record
        route.fulfill(response=response, json=payload)

    def detail_route(route):
        if parse_qs(urlparse(route.request.url).query).get("id") != [
            record["id"]
        ]:
            route.continue_()
            return
        route.fulfill(status=200, json={"ok": True, "data": {"entry": record}})

    page.route("**/api/ui/audit?*", records_route)
    page.route("**/api/ui/audit/detail?*", detail_route)
    harness.navigate("audit")
    detail = page.locator("#audit-detail-body")
    expect(detail).to_have_attribute("data-audit-entry-id", "react-detail-real")
    expect(detail).to_have_attribute("data-expanded-pane", "split")
    expect(detail.locator(".audit-call-panel")).to_have_count(2)
    expect(detail.locator(".audit-call-panel-heading").first).to_have_attribute(
        "aria-expanded", "true"
    )
    request = detail.locator(".audit-call-panel").first
    result = detail.locator(".audit-call-panel").nth(1)
    expect(request.locator(".audit-tree-branch").first).to_be_visible()
    expect(result).to_contain_text("write completed")
    page.locator("#audit-list .audit-entry").first.click()
    expect(detail.locator(".audit-call-panel")).to_have_count(2)
    page.locator("#audit-refresh").click()
    expect(detail).to_have_attribute("data-audit-entry-id", "react-detail-real")

    # Isolated global and session mounts check the exact same rendering logic,
    # without coupling to a particular Audit backend's stored event history.
    page.evaluate("""() => {
      window.__auditedXss = 0;
      window.__auditedCopy = '';
      Object.defineProperty(navigator, 'clipboard', { configurable: true,
        value: { writeText: async text => { window.__auditedCopy = text; } } });
      for (const scope of ['global', 'session']) {
        const host = document.createElement('div');
        host.id = `detail-${scope}-contract`;
        host.className = 'audit-detail-body';
        document.getElementById('app-main').append(host);
        window[`__auditDetail_${scope}`] = window.WorkgateReactAuditDetails.mount(
          host, n => `${n} B`
        );
      }
      window.__detailRecord = {
        id:'same-record', tool:'read_file', event:'call',
        input: { path:'<img src=x onerror="window.__auditedXss=1">',
          args:{numbers:[1,2,3],flag:false} },
        output: { nested: { value: 3, items:[1,2] },
          rows: Array.from({length:80}, (_,i) => `line-${i}`) },
        related_events:[{kind:'response'}], extra_field:'supplemental'
      };
      window.__auditDetail_global.renderEntry(window.__detailRecord);
      window.__auditDetail_session.renderEntry({
        id:'different-session', input:{text:'session independent'}, output:'raw text',
        image_preview:{mime_type:'image/png', data_base64:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+mWN0AAAAASUVORK5CYII=',
          path:'capture.png',bytes:68}
      });
    }""")
    global_detail = page.locator("#detail-global-contract")
    session_detail = page.locator("#detail-session-contract")
    expect(global_detail.locator(".audit-call-panel")).to_have_count(2)
    expect(session_detail.locator(".audit-call-panel")).to_have_count(2)
    expect(global_detail).to_have_attribute("data-expanded-pane", "split")
    expect(
        session_detail.locator(".audit-image-preview img")
    ).to_have_attribute(
        "src",
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+mWN0AAAAASUVORK5CYII=",
    )
    expect(
        session_detail.locator(".audit-image-preview figcaption")
    ).to_have_text("capture.png (68 B)")
    global_request = global_detail.locator(".audit-call-panel").first
    global_result = global_detail.locator(".audit-call-panel").nth(1)
    expect(global_request).to_contain_text("<img src=x onerror=")
    assert global_request.locator("img").count() == 0
    assert page.evaluate("window.__auditedXss") == 0
    expect(global_result).to_contain_text("supplemental")
    expect(global_result).to_contain_text("response")

    # Pane/Raw state, nested tree disclosure, body focus and scroll must remain
    # attached to the SAME DOM nodes when the record is refreshed/updated.
    global_request.get_by_role("button", name="Raw").click()
    expect(global_request.get_by_role("button", name="Raw")).to_have_attribute(
        "aria-pressed", "true"
    )
    global_result.get_by_role("button", name="Call result").click()
    expect(global_detail).to_have_attribute("data-expanded-pane", "result")
    nested = global_result.locator("details.audit-tree-branch").nth(1)
    nested.locator("summary").first.click()
    expect(nested).to_have_attribute("open", "")
    global_result.get_by_role("button", name="Raw").click()
    expect(global_result.locator(".audit-detail-json")).to_be_visible()
    global_result.get_by_role("button", name="Tree").click()
    expect(nested).to_have_attribute("open", "")
    result_body = global_result.locator(".audit-call-panel-body")
    result_body.evaluate(
        "el => { el.style.maxHeight='45px'; el.scrollTop=40; }"
    )
    result_body.focus()
    expect(result_body).to_be_focused()
    scroll_before = result_body.evaluate("el => el.scrollTop")
    assert scroll_before > 0
    page.evaluate("""() => {
      window.__oldDetailPanel = document.querySelector('#detail-global-contract .audit-call-panel');
      window.__oldResultBody = document.querySelectorAll('#detail-global-contract .audit-call-panel-body')[1];
      window.__oldExpandedTree = document.querySelectorAll('#detail-global-contract .audit-call-panel:nth-child(2) details.audit-tree-branch')[1];
      window.__detailMutations = 0;
      window.__detailMutationRecords = [];
      new MutationObserver(records => {
        for (const record of records) {
          window.__detailMutations += record.type === 'attributes' ? 1 : record.addedNodes.length + record.removedNodes.length;
          window.__detailMutationRecords.push({type:record.type, attribute:record.attributeName,
            target:record.target.outerHTML?.slice(0,260),old:record.oldValue});
        }
      }).observe(document.getElementById('detail-global-contract'), {
        subtree:true, attributes:true, childList:true
      });
      window.__auditDetail_global.renderEntry({...window.__detailRecord});
    }""")
    page.wait_for_timeout(100)
    assert page.evaluate("window.__detailMutations") == 0, page.evaluate(
        "window.__detailMutationRecords"
    )
    assert global_result.locator(".audit-call-panel-body").evaluate(
        "el => el === window.__oldResultBody"
    )
    expect(result_body).to_be_focused()
    assert result_body.evaluate("el => el.scrollTop") == scroll_before
    expect(global_detail).to_have_attribute("data-expanded-pane", "result")
    expect(nested).to_have_attribute("open", "")
    expect(
        global_request.locator(".audit-value-toolbar button").nth(1)
    ).to_have_attribute("aria-pressed", "true")

    # Same selected record, changed output: keep interaction state while
    # updating content; selecting another ID resets the layout to split.
    page.evaluate("""() => {
      window.__auditDetail_global.renderEntry({ ...window.__detailRecord,
        output: {...window.__detailRecord.output,
          nested:{...window.__detailRecord.output.nested, updated: true}}
      });
    }""")
    expect(global_result).to_contain_text("updated")
    assert global_result.locator(".audit-call-panel-body").evaluate(
        "el => el === window.__oldResultBody"
    )
    expect(global_detail).to_have_attribute("data-expanded-pane", "result")
    expect(nested).to_have_attribute("open", "")
    expect(result_body).to_be_focused()
    assert result_body.evaluate("el => el.scrollTop") == scroll_before
    page.evaluate("""() => window.__auditDetail_global.renderEntry({
      id:'next-record', input:'non-JSON',output:{ok:false,error:'next error'}
    })""")
    expect(global_detail).to_have_attribute(
        "data-audit-entry-id", "next-record"
    )
    expect(global_detail).to_have_attribute("data-expanded-pane", "split")
    expect(
        global_request.locator(".audit-value-toolbar button").first
    ).to_be_hidden()
    expect(global_request.locator(".audit-detail-json")).to_be_visible()
    expect(session_detail).to_have_attribute(
        "data-audit-entry-id", "different-session"
    )

    # Clipboard success/error, image failure and session teardown.
    global_result.get_by_role("button", name="Copy").click()
    expect(global_result.get_by_role("button", name="Copied")).to_be_visible()
    assert "next error" in page.evaluate("window.__auditedCopy")
    page.evaluate("""() => {
      Object.defineProperty(navigator, 'clipboard', {configurable:true,
        value:{writeText:async()=>{throw new Error('blocked')}}});
    }""")
    global_request.get_by_role("button", name="Copy").click()
    expect(
        global_request.get_by_role("button", name="Copy failed")
    ).to_be_visible()
    page.evaluate("""() => window.__auditDetail_session.renderEntry({
      id:'different-session', input:{message:'test'}, output:{ok:true},
      image_preview_error:'image too large'
    })""")
    expect(session_detail.locator(".audit-image-preview")).to_have_count(0)
    expect(session_detail.locator(".audit-preview-error")).to_contain_text(
        "image too large"
    )
    page.evaluate(
        """() => window.__auditDetail_session.renderMessage('Session access revoked')"""
    )
    expect(session_detail.locator(".audit-call-panel")).to_have_count(0)
    expect(session_detail.locator(".audit-detail-message")).to_have_text(
        "Session access revoked"
    )
    assert session_detail.get_attribute("data-audit-entry-id") is None
    assert session_detail.get_attribute("data-expanded-pane") is None
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
