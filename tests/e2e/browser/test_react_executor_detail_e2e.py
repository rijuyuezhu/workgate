"""Authenticated and synthetic Chromium contracts for React Executor details."""

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


def test_react_executor_detail_actions_and_updates(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    page.goto(f"{harness.base_url}/ui#executors")
    assert page.evaluate(
        "typeof window.WorkgateReactExecutorDetail?.mount === 'function'"
    )
    expect(page.locator("#executor-detail-name")).to_have_text("Control")
    expect(page.locator("#executor-control-overview")).to_be_visible()
    expect(page.locator("#executor-detail-grid")).to_be_hidden()
    expect(page.locator(".executor-detail-actions")).to_be_hidden()
    expect(page.locator("#executor-control-online")).to_have_text("1")

    page.locator(
        f'#executor-list [data-selection="{harness.executor_id}"]'
    ).click()
    expect(page.locator("#executor-detail-id")).to_have_text(
        harness.executor_id
    )
    expect(page.locator("#executor-control-overview")).to_be_hidden()
    expect(page.locator("#executor-detail-grid")).to_be_visible()
    expect(page.locator("#executor-rename-open")).to_be_enabled()
    expect(page.locator("#executor-drain")).to_have_text("Drain")
    page.locator("#executor-rename-open").click()
    expect(page.locator("#executor-rename-dialog")).to_be_visible()
    expect(page.locator("#executor-rename-name")).to_be_focused()
    page.locator("#executor-rename-dialog [aria-label='Close']").click()
    page.locator("#executor-revoke-open").click()
    expect(page.locator("#executor-revoke-dialog")).to_be_visible()
    page.locator("#executor-revoke-dialog [aria-label='Close']").click()
    page.locator('[data-executor-subtab="mcp"]').click()
    expect(page.locator('[data-executor-subtab="mcp"]')).to_have_attribute(
        "aria-selected", "true"
    )
    page.locator('[data-executor-subtab="overview"]').click()
    expect(page.locator("#executor-detail-grid")).to_be_visible()

    # Independent React view exercises denied/offline/draining and no DOM churn.
    page.evaluate("""() => {
      const header = document.createElement('div');
      header.id = 'executor-detail-test-header';
      const overview = document.createElement('div');
      overview.id = 'executor-detail-test-overview';
      document.getElementById('app-main').append(header, overview);
      window.__detailActions = [];
      window.__detailTest = window.WorkgateReactExecutorDetail.mount(
        header, overview, action => window.__detailActions.push(action)
      );
      window.__detailData = {
        control:false, available:true, stale:false, onlineCount:2,
        executor: {
          executor_id:'test-executor',name:'Sample Executor',online:true,
          runtime:{workgate_version:'6.0',ownership:'user',platform:'linux'},
          session_admission:true,active_sessions:3,queued_commands:1,
          offered_commands:2,command_limit:5,capabilities:['shell','gui']
        }
      };
      window.__detailTest.renderDetails(window.__detailData);
      window.__detailTest.renderControls(false);
    }""")
    header = page.locator("#executor-detail-test-header")
    overview = page.locator("#executor-detail-test-overview")
    expect(header.locator("#executor-detail-name")).to_have_text(
        "Sample Executor"
    )
    expect(header.locator("#executor-detail-status")).to_have_text("online")
    expect(overview.locator("#executor-detail-version")).to_have_text("6.0")
    expect(overview.locator("#executor-detail-runtime-owner")).to_have_text(
        "user"
    )
    expect(overview.locator("#executor-detail-admission")).to_have_text(
        "Eligible"
    )
    expect(overview.locator("#executor-detail-sessions")).to_have_text("3")
    expect(overview.locator("#executor-detail-commands")).to_have_text(
        "1 queued, 2 offered (limit 5)"
    )
    for action in ("reset", "drain", "rename", "revoke"):
        header.locator(
            f"#executor-{action if action != 'rename' and action != 'revoke' else action + '-open'}"
        ).click()
    assert page.evaluate("window.__detailActions") == [
        "reset",
        "drain",
        "rename",
        "revoke",
    ]

    # Exactly the same snapshots must keep the focused action DOM untouched.
    rename = header.locator("#executor-rename-open")
    rename.focus()
    expect(rename).to_be_focused()
    page.evaluate("""() => {
      window.__focusedDetailAction = document.activeElement;
      window.__detailMutations = 0;
      new MutationObserver(records => {
        for (const record of records) {
          window.__detailMutations += record.type === 'attributes' ? 1 :
            record.addedNodes.length + record.removedNodes.length;
        }
      }).observe(document.getElementById('executor-detail-test-header'), {
        subtree:true, attributes:true, childList:true
      });
      window.__detailTest.renderDetails({...window.__detailData});
      window.__detailTest.renderControls(false);
    }""")
    page.wait_for_timeout(120)
    assert page.evaluate("window.__detailMutations") == 0
    assert rename.evaluate("el => el === window.__focusedDetailAction")
    expect(rename).to_be_focused()

    page.evaluate("""() => {
      window.__detailTest.renderDetails({
        ...window.__detailData,
        executor: {...window.__detailData.executor, draining:true, online:false,
          session_admission:false, session_admission_reasons:['offline']}
      });
    }""")
    expect(header.locator("#executor-detail-status")).to_have_text("draining")
    expect(header.locator("#executor-drain")).to_have_text("Resume")
    expect(overview.locator("#executor-detail-admission")).to_have_text(
        "offline"
    )
    page.evaluate("""() => window.__detailTest.renderControls(true)""")
    expect(header.locator("#executor-rename-open")).to_be_disabled()
    page.evaluate("""() => {
      window.__detailTest.renderDetails({
        control:true,available:false,stale:true,onlineCount:0,executor:null
      });
    }""")
    expect(header.locator("#executor-detail-name")).to_have_text("Control")
    expect(header.locator(".executor-detail-actions")).to_be_hidden()
    expect(overview.locator("#executor-detail-grid")).to_be_hidden()
    expect(overview.locator("#executor-control-connection")).to_have_text(
        "Stale"
    )
    expect(overview.locator("#executor-control-online")).to_have_text("0")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
