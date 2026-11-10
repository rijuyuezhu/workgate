"""Authenticated Plan editing and stable React Todo rows with controller-owned saves."""

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


def test_react_task_plan_editing(browser_harness: BrowserHarness) -> None:
    harness = browser_harness
    harness.login()
    page = harness.page
    assert page.evaluate(
        "typeof window.WorkgateReactTaskPlan?.mount === 'function'"
    )
    result = harness.api(
        "POST",
        "/tools/task",
        body={"action": "create", "label": "react-plan-edit"},
    )
    assert result["status"] == 200 and not result["payload"].get("isError"), (
        result
    )
    task_id = result["payload"]["structuredContent"]["result"]["task_id"]
    page.goto(f"{harness.base_url}/ui?task_id={task_id}#tasks")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#task-state")).to_have_text(task_id)
    page.locator("#task-tab-plan").click()
    expect(page.locator("#todo-summary span").first).to_have_text("0 total")
    expect(page.locator("#todo-list .empty-state")).to_contain_text(
        "No plan steps"
    )

    page.locator("#todo-add").click()
    rows = page.locator("#todo-list .todo-row")
    expect(rows).to_have_count(1)
    first = rows.first
    first_input = first.locator("input")
    expect(first_input).to_be_focused()
    first_input.fill("Implement cache checks")
    expect(first_input).to_have_value("Implement cache checks")
    expect(page.locator("#todo-state")).to_have_text("Unsaved changes")
    first.locator("select").first.select_option("in_progress")
    expect(first).to_have_attribute("data-status", "in_progress")
    first.locator("select").nth(1).select_option("high")
    expect(first).to_have_attribute("data-priority", "high")
    page.locator("#todo-add").click()
    expect(rows).to_have_count(2)
    second = rows.nth(1)
    second.locator("input").fill("Remove this step")
    second.get_by_role("button", name="Remove").click()
    expect(rows).to_have_count(1)
    expect(first.locator("input")).to_have_value("Implement cache checks")

    # Focus, selection and DOM identity survive a typed change in the middle.
    first_input.focus()
    first_input.press("Home")
    first_input.press("ArrowRight")
    first_input.press("x")
    expect(first_input).to_have_value("Ixmplement cache checks")
    expect(first_input).to_be_focused()

    page.locator("#todo-save").click()
    expect(page.locator("#todo-state")).to_have_text("Saved")
    persisted = harness.api("GET", f"/api/ui/todos?task_id={task_id}")
    assert persisted["status"] == 200, persisted
    persisted_todos = persisted["payload"]["data"]["todos"]
    assert len(persisted_todos) == 1, persisted_todos
    assert persisted_todos[0]["content"] == "Ixmplement cache checks"
    assert persisted_todos[0]["status"] == "in_progress"
    assert persisted_todos[0]["priority"] == "high"

    # Changing status updates counts and filtering; switching back restores
    # exactly the same keyed field, independent of its visible row index.
    first.locator("select").first.select_option("completed")
    expect(page.locator("#todo-summary span").nth(2)).to_have_text(
        "1 completed"
    )
    page.locator("#todo-filter").select_option("open")
    expect(rows).to_have_count(0)
    expect(page.locator("#todo-list .empty-state")).to_contain_text(
        "No plan steps match"
    )
    page.locator("#todo-filter").select_option("completed")
    expect(rows).to_have_count(1)
    expect(first.locator("input")).to_have_value("Ixmplement cache checks")
    expect(page.locator("#todo-save")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator("#todo-refresh").click()
    expect(rows).to_have_count(1)
    expect(page.locator("#todo-state")).to_have_text("Unsaved changes")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#todo-refresh").click()
    expect(page.locator("#todo-state")).not_to_have_text("Unsaved changes")
    page.locator("#todo-filter").select_option("all")
    expect(first.locator("select").first).to_have_value("in_progress")
    expect(page.locator("#todo-summary span").nth(1)).to_have_text("1 open")

    # Isolated client view proves no redundant DOM writes, stable row IDs,
    # proper option fallback and safe text regardless of API content.
    page.evaluate("""() => {
      const summary=document.createElement('div');summary.id='plan-react-summary';
      const list=document.createElement('div');list.id='plan-react-list';
      document.getElementById('app-main').append(summary,list);
      window.__planActions=[];
      window.__planView=window.WorkgateReactTaskPlan.mount(summary,list,{
        content:(id,value)=>window.__planActions.push(['content',id,value]),
        status:(id,value)=>window.__planActions.push(['status',id,value]),
        priority:(id,value)=>window.__planActions.push(['priority',id,value]),
        remove:id=>window.__planActions.push(['remove',id]),
      });
      window.__planModel={
        rows:[
          {id:'step-1',index:0,content:'<img src=x onerror="window.__planXss=1">',status:'in_progress',priority:'medium'},
          {id:'step-2',index:1,content:'Write summary',status:'unexpected_status',priority:'urgent'}
        ],counts:{total:2,open:2,completed:0,skipped:0},emptyMessage:'No steps',contentMaxLength:800,
      };
      window.__planXss=0;
      window.__planView.render(window.__planModel);
    }""")
    synthetic = page.locator("#plan-react-list")
    expect(synthetic.locator(".todo-row")).to_have_count(2)
    expect(
        synthetic.locator('[data-todo-id="step-2"] select').first
    ).to_have_value("unexpected_status")
    expect(
        synthetic.locator('[data-todo-id="step-2"] select').nth(1)
    ).to_have_value("urgent")
    expect(synthetic.locator('[data-todo-id="step-1"] input')).to_have_value(
        '<img src=x onerror="window.__planXss=1">'
    )
    assert synthetic.locator("img").count() == 0
    assert page.evaluate("window.__planXss") == 0
    synthetic.evaluate(
        "el=>{el.style.height='44px';el.style.overflowY='scroll';el.scrollTop=18;}"
    )
    synthetic_input = synthetic.locator('[data-todo-id="step-2"] input')
    synthetic_input.focus()
    expect(synthetic_input).to_be_focused()
    scroll_before = synthetic.evaluate("el=>el.scrollTop")
    assert scroll_before > 0
    page.evaluate("""() => {
      window.__planFocused=document.activeElement;
      window.__planChanges=0;
      for(const id of ['plan-react-summary','plan-react-list']) {
        new MutationObserver(records=>{
          for(const r of records) window.__planChanges +=
            r.type==='attributes'?1:r.addedNodes.length+r.removedNodes.length;
        }).observe(document.getElementById(id),{subtree:true,attributes:true,childList:true});
      }
      window.__planView.render({...window.__planModel,
        rows:window.__planModel.rows.map(item=>({...item}))});
    }""")
    assert page.evaluate("window.__planChanges") == 0
    assert synthetic_input.evaluate("el=>el===window.__planFocused")
    assert synthetic.evaluate("el=>el.scrollTop") == scroll_before
    page.evaluate("""() => window.__planView.render({...window.__planModel,
      rows:[window.__planModel.rows[1],window.__planModel.rows[0]]})""")
    assert synthetic_input.evaluate("el=>el===window.__planFocused")
    page.evaluate("""() => window.__planView.render({...window.__planModel,
      rows:[],emptyMessage:'Access revoked'})""")
    expect(synthetic.locator(".empty-state")).to_have_text("Access revoked")
    expect(synthetic.locator(".todo-row")).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
