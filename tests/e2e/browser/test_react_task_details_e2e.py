"""React read-only Task Progress and Session identity/metadata browser contracts."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(150),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_react_task_progress_and_session_details(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    assert page.evaluate(
        "typeof window.WorkgateReactTaskDetails?.mount === 'function'"
    )

    # Actual authenticated controller, real Task and linked Session.
    created = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "react-detail-task"},
    )
    assert created["status"] == 200 and not created["payload"].get("isError"), (
        created
    )
    task_id = created["payload"]["structuredContent"]["result"]["task_id"]
    session = harness.api(
        "POST",
        "/tools/session_start",
        body={
            "executor_id": harness.executor_id,
            "task_id": task_id,
            "workdir": ".",
        },
    )
    assert session["status"] == 200 and not session["payload"].get("isError"), (
        session
    )
    session_id = session["payload"]["structuredContent"]["session_id"]
    page.goto(
        f"{harness.base_url}/ui?task_id={task_id}&session_id={session_id}#tasks"
    )
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#task-state")).to_have_text(task_id)
    expect(page.locator("#task-status")).to_have_text("active")
    expect(page.locator("#task-status")).to_have_attribute(
        "data-status", "active"
    )
    expect(page.locator("#task-objective")).not_to_be_empty()
    expect(page.locator(".task-progress-findings")).to_be_hidden()
    page.locator("#task-tab-sessions").click()
    expect(page.locator("#session-detail-id")).to_have_text(session_id)
    expect(page.locator("#session-detail-executor-id")).to_have_text(
        harness.executor_id
    )
    expect(page.locator("#session-detail-title")).not_to_be_empty()
    expect(page.locator("#session-detail-executor-avatar")).not_to_be_empty()
    expect(page.locator("#session-detail-status")).to_contain_text("Available")
    page.locator(".task-session-metadata summary").click()
    expect(page.locator(".task-session-metadata")).to_have_attribute("open", "")
    page.locator("#tasks-refresh").click()
    expect(page.locator("#session-detail-id")).to_have_text(session_id)
    expect(page.locator(".task-session-metadata")).to_have_attribute("open", "")
    expect(page.locator('[data-task-action="finish"]')).to_be_enabled()

    # Independent view instance: identities, safe text, unchanged reconciling,
    # updated same-session metadata, Task status/arrays, and session reset.
    page.evaluate("""() => {
      const wrap = document.createElement('div');
      wrap.id='task-details-react-contract';
      wrap.innerHTML='<div class="task-progress-lead"></div><dl class="session-task-grid"></dl>' +
        '<div class="session-detail-heading"></div><details class="task-session-metadata"><summary>Session metadata</summary>'+
        '<dl class="session-detail-grid"></dl></details>';
      document.getElementById('app-main').append(wrap);
      const roots = Array.from(wrap.querySelectorAll('.task-progress-lead,.session-task-grid,.session-detail-heading,.session-detail-grid'));
      window.__taskDetailsRoots = roots;
      window.__taskDetailsView = window.WorkgateReactTaskDetails.mount(...roots);
      window.__taskDetailsModel = {
        objective:'<img src=x onerror="window.__taskDetailsXss=1">',
        status:'blocked',summary:'Summarized safely',nextAction:'Retry later',
        findings:'• First fact\\n• Second fact',blockers:'• Blocked by dependency',
        hasFindings:true,hasBlockers:true
      };
      window.__sessionDetailsModel = {
        executor:'Spark',title:'Session Label',status:'Available (today)',state:'available',
        taskCreated:'Created Task task-auto automatically',sessionId:'session-a',
        executorId:'exec-a',workdir:'/tmp/sample',created:'Today',updated:'Now'
      };
      window.__taskDetailsXss=0;
      window.__taskDetailsView.renderTask(window.__taskDetailsModel);
      window.__taskDetailsView.renderSession(window.__sessionDetailsModel);
    }""")
    wrap = page.locator("#task-details-react-contract")
    expect(wrap.locator("#task-objective")).to_contain_text("<img src=x")
    assert wrap.locator("#task-objective img").count() == 0
    assert page.evaluate("window.__taskDetailsXss") == 0
    expect(wrap.locator("#task-status")).to_have_attribute(
        "data-status", "blocked"
    )
    expect(wrap.locator(".task-progress-findings")).to_be_visible()
    expect(wrap.locator("#task-findings")).to_contain_text("Second fact")
    expect(wrap.locator("#session-detail-task-created")).to_be_visible()
    expect(wrap.locator("#session-detail-task-created")).to_have_text(
        "Created Task task-auto automatically"
    )
    expect(wrap.locator("#session-detail-executor-avatar")).to_have_text("S")
    expect(wrap.locator("#session-detail-status")).to_have_attribute(
        "data-state", "available"
    )
    details = wrap.locator(".task-session-metadata")
    details.locator("summary").click()
    expect(details).to_have_attribute("open", "")
    metadata = wrap.locator(".session-detail-grid")
    metadata.evaluate(
        "el => {el.style.maxHeight='32px';el.style.overflowY='scroll';el.scrollTop=20;}"
    )
    focus = wrap.locator("#session-detail-executor-id")
    focus.evaluate("el => {el.tabIndex=0;el.focus({preventScroll:true});}")
    expect(focus).to_be_focused()
    scroll_before = metadata.evaluate("el => el.scrollTop")
    assert scroll_before > 0

    # Identical snapshots must not mutate the DOM, including ARIA/status bits.
    page.evaluate("""() => {
      window.__taskDetailsFocus = document.activeElement;
      window.__taskDetailsDisclosure = document.querySelector('#task-details-react-contract details');
      window.__taskDetailsMutationCount=0;
      for (const root of window.__taskDetailsRoots) {
        new MutationObserver(records => {
          for (const r of records) window.__taskDetailsMutationCount +=
            r.type==='attributes'?1:r.addedNodes.length+r.removedNodes.length;
        }).observe(root,{subtree:true,attributes:true,childList:true});
      }
      window.__taskDetailsView.renderTask({...window.__taskDetailsModel});
      window.__taskDetailsView.renderSession({...window.__sessionDetailsModel});
    }""")
    assert page.evaluate("window.__taskDetailsMutationCount") == 0
    expect(focus).to_be_focused()
    assert focus.evaluate("el => el === window.__taskDetailsFocus")
    assert details.evaluate("el => el === window.__taskDetailsDisclosure")
    assert metadata.evaluate("el => el.scrollTop") == scroll_before

    page.evaluate("""() => {
      window.__taskDetailsView.renderTask({ ...window.__taskDetailsModel,
        status:'completed', findings:'—',blockers:'—',hasFindings:false,hasBlockers:false
      });
      window.__taskDetailsView.renderSession({...window.__sessionDetailsModel,
        executor:'HP',status:'Unavailable (executor offline)',state:'warning',
        taskCreated:'',updated:'Later'
      });
    }""")
    expect(wrap.locator("#task-status")).to_have_attribute(
        "data-status", "completed"
    )
    expect(wrap.locator(".task-progress-findings")).to_be_hidden()
    expect(wrap.locator(".task-progress-blockers")).to_be_hidden()
    expect(wrap.locator("#session-detail-status")).to_contain_text(
        "executor offline"
    )
    expect(wrap.locator("#session-detail-status")).to_have_attribute(
        "data-state", "warning"
    )
    expect(wrap.locator("#session-detail-task-created")).to_be_hidden()
    expect(wrap.locator("#session-detail-executor-avatar")).to_have_text("H")
    expect(wrap.locator("#session-detail-updated")).to_have_text("Later")
    expect(details).to_have_attribute("open", "")
    expect(focus).to_be_focused()
    assert metadata.evaluate("el => el.scrollTop") == scroll_before
    page.evaluate("""() => {
      window.__taskDetailsView.renderTask();
      window.__taskDetailsView.renderSession();
    }""")
    expect(wrap.locator("#session-detail-title")).to_have_text(
        "No session selected"
    )
    expect(wrap.locator("#session-detail-id")).to_have_text("—")
    expect(wrap.locator("#session-detail-status")).to_have_attribute(
        "data-state", "other"
    )
    expect(wrap.locator("#task-status")).to_have_attribute(
        "data-status", "other"
    )
    assert page.evaluate("window.__taskDetailsXss") == 0
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
