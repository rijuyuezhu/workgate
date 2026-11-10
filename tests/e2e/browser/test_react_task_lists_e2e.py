"""Authenticated Tasks navigation and stable shared React Task/Session inventories."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(160),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_react_task_session_inventories(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    assert page.evaluate(
        "typeof window.WorkgateReactTaskLists?.mount === 'function'"
    )

    task = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "react-lists-task"},
    )
    assert task["status"] == 200 and not task["payload"].get("isError"), task
    task_id = task["payload"]["structuredContent"]["result"]["task_id"]
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
    task_row = page.locator(f'#task-list .task-entry[data-task-id="{task_id}"]')
    session_row = page.locator(
        f'#session-list .session-entry[data-session-id="{session_id}"]'
    )
    expect(task_row).to_be_visible()
    expect(session_row).to_have_attribute("aria-current", "true")
    expect(task_row).to_have_attribute("aria-current", "true")
    expect(task_row.locator(".task-entry-heading strong")).to_have_text(
        "react-lists-task"
    )
    expect(session_row.locator(".executor-band-mark")).not_to_be_empty()
    page.locator("#task-tab-sessions").click()
    expect(session_row).to_be_visible()
    session_row.click()
    expect(session_row).to_have_attribute("aria-current", "true")
    page.locator("#task-list").evaluate(
        "el => { el.style.maxHeight = '65px'; el.scrollTop = 12; }"
    )
    task_row.focus()
    expect(task_row).to_be_focused()
    page.evaluate("""() => {
      window.__realTaskRow = document.activeElement;
      window.__taskInventoryMutationCount = 0;
      new MutationObserver(records => {
        for (const record of records) window.__taskInventoryMutationCount +=
          record.type === 'attributes' ? 1 : record.addedNodes.length + record.removedNodes.length;
      }).observe(document.getElementById('task-list'), {subtree:true, attributes:true, childList:true});
    }""")
    with page.expect_response(lambda r: "/api/ui/tasks?" in r.url):
        page.evaluate("() => document.getElementById('tasks-refresh').click()")
    expect(task_row).to_have_attribute("aria-current", "true")
    page.wait_for_timeout(120)
    expect(task_row).to_be_focused()
    assert task_row.evaluate("el => el === window.__realTaskRow")
    assert page.evaluate("window.__taskInventoryMutationCount") == 0

    # Independent mounts: task status, unattached/attached session records,
    # Executor name changes, disabled/retired rows and keyed focus/scroll.
    page.evaluate("""() => {
      const tasks = document.createElement('nav');
      tasks.id='react-tasks-contract';tasks.className='session-list';
      const sessions = document.createElement('nav');
      sessions.id='react-sessions-contract';sessions.className='task-session-list';
      document.getElementById('app-main').append(tasks, sessions);
      window.__taskClicks={task:[],unattached:[],session:[]};
      window.__taskView = window.WorkgateReactTaskLists.mount(tasks,sessions,{
        task:id=>window.__taskClicks.task.push(id),
        unattached:id=>window.__taskClicks.unattached.push(id),
        session:id=>window.__taskClicks.session.push(id),
      });
      window.__taskModel={
        tasks:[
          {id:'t-1',title:'Task A',status:'active',meta:'2 sessions (updated today)'},
          {id:'t-2',title:'Task B',status:'blocked',meta:'0 sessions (updated today)'},
          {id:'t-3',title:'Task C',status:'completed',meta:'1 session (updated yesterday)'}
        ],
        unattached:[
          {id:'u-1',title:'Detached (u-1)',meta:'available (today)',executor:'Spark',tooltip:'Detached — Spark'},
          {id:'u-2',title:'Old (u-2)',meta:'ended (yesterday)',executor:'DOM',tooltip:'Old — DOM'}
        ],selectedTaskId:'t-2',selectedSessionId:'',emptyMessage:'No matches'
      };
      window.__sessionModel={
        sessions:[
          {id:'s-1',title:'Attached session',meta:'available (today)',executor:'Spark',tooltip:'Attached — Spark'},
          {id:'s-2',title:'Ended session',meta:'ended (yesterday)',executor:'DOM',terminated:true,tooltip:'Ended — DOM'}
        ],selectedSessionId:'s-1',emptyMessage:'No sessions'
      };
      window.__taskView.renderTasks(window.__taskModel);
      window.__taskView.renderSessions(window.__sessionModel);
    }""")
    tasks = page.locator("#react-tasks-contract")
    sessions = page.locator("#react-sessions-contract")
    expect(tasks.locator(".task-entry")).to_have_count(3)
    expect(tasks.locator(".unattached-session-entry")).to_have_count(2)
    expect(sessions.locator(".session-entry")).to_have_count(2)
    expect(tasks.locator(".session-group-label")).to_have_text(
        "Unattached sessions"
    )
    expect(tasks.locator('[data-task-id="t-2"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(
        tasks.locator('[data-task-id="t-2"] .task-entry-status')
    ).to_have_attribute("data-status", "blocked")
    expect(
        sessions.locator('[data-session-id="s-2"] .session-entry-meta')
    ).to_have_class("session-entry-meta session-entry-terminated")
    expect(
        sessions.locator('[data-session-id="s-1"] .executor-band-mark')
    ).to_have_text("S")

    # Synchronous keyed React renders must be mutation-free for equal snapshots.
    focused_task = tasks.locator('[data-task-id="t-2"]')
    focused_session = sessions.locator('[data-session-id="s-1"]')
    tasks.evaluate(
        "el => {el.style.height='45px';el.style.overflowY='scroll';el.scrollTop=35;}"
    )
    focused_task.focus()
    expect(focused_task).to_be_focused()
    scroll_before = tasks.evaluate("el => el.scrollTop")
    page.evaluate("""() => {
      window.__syntheticFocusedTask=document.activeElement;
      window.__syntheticTaskChanges=0;
      window.__syntheticSessionChanges=0;
      for (const [id, counter] of [
        ['react-tasks-contract','__syntheticTaskChanges'],
        ['react-sessions-contract','__syntheticSessionChanges']
      ]) {
        new MutationObserver(records=>{
          for(const r of records) window[counter]+=r.type==='attributes'?1:r.addedNodes.length+r.removedNodes.length;
        }).observe(document.getElementById(id),{subtree:true,attributes:true,childList:true});
      }
      window.__taskView.renderTasks({ ...window.__taskModel, tasks:window.__taskModel.tasks.map(t=>({...t})) });
      window.__taskView.renderSessions({ ...window.__sessionModel, sessions:window.__sessionModel.sessions.map(s=>({...s})) });
    }""")
    assert page.evaluate("window.__syntheticTaskChanges") == 0
    assert page.evaluate("window.__syntheticSessionChanges") == 0
    expect(focused_task).to_be_focused()
    assert focused_task.evaluate("el => el === window.__syntheticFocusedTask")
    assert tasks.evaluate("el => el.scrollTop") == scroll_before

    # Executor rename/offline changes metadata on existing keyed DOM rows.
    page.evaluate("""() => {
      window.__syntheticSession = document.querySelector('#react-sessions-contract [data-session-id="s-1"]');
      window.__taskView.renderSessions({...window.__sessionModel, sessions:[
        {...window.__sessionModel.sessions[0],executor:'HP',meta:'executor offline (today)',tooltip:'Attached — HP'},
        window.__sessionModel.sessions[1]
      ]});
    }""")
    assert focused_session.evaluate("el => el === window.__syntheticSession")
    expect(focused_session.locator(".executor-band-mark")).to_have_text("H")
    expect(focused_session.locator(".session-entry-meta")).to_contain_text(
        "offline"
    )
    focused_session.click()
    tasks.locator('[data-session-id="u-1"]').click()
    tasks.locator('[data-task-id="t-1"]').click()
    assert page.evaluate("window.__taskClicks") == {
        "task": ["t-1"],
        "unattached": ["u-1"],
        "session": ["s-1"],
    }

    page.evaluate("""() => {
      window.__taskView.renderTasks({ ...window.__taskModel,
        tasks:window.__taskModel.tasks.filter(t=>t.id!=='t-2'),
        unattached:[], selectedTaskId:'t-1' });
      window.__taskView.renderSessions({sessions:[],selectedSessionId:'',emptyMessage:'No retained sessions'});
    }""")
    expect(tasks.locator('[data-task-id="t-1"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(tasks.locator('[data-task-id="t-2"]')).to_have_count(0)
    expect(sessions.locator(".empty-state")).to_have_text(
        "No retained sessions"
    )
    page.evaluate("""() => window.__taskView.renderTasks({
      tasks:[],unattached:[],selectedTaskId:'',selectedSessionId:'',emptyMessage:'Access revoked'
    })""")
    expect(tasks.locator(".task-entry")).to_have_count(0)
    expect(tasks.locator(".empty-state")).to_have_text("Access revoked")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
