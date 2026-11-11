"""Files Executor locations and path breadcrumbs use stable scoped React rows."""

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


def test_react_files_navigation_real_and_keyed(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    assert page.evaluate(
        "typeof window.WorkgateReactFilesNavigation?.mount === 'function'"
    )

    directory = harness.executor_workspace / "react-files-nav-directory"
    directory.mkdir(exist_ok=True)
    (directory / "inside.txt").write_text("inside React path", encoding="utf-8")
    harness.navigate("files")
    location = page.locator(
        f'#file-location-executors [data-executor-id="{harness.executor_id}"]'
    )
    expect(location).to_be_visible()
    expect(location).to_have_attribute("aria-current", "true")
    expect(location).to_be_enabled()
    page.locator("#file-path").fill(directory.name)
    page.locator("#file-path-form").get_by_role("button", name="Go").click()
    expect(page.locator("#file-breadcrumbs")).to_contain_text(directory.name)
    expect(page.locator(".wg-files-list")).to_contain_text("inside.txt")
    expect(
        page.locator("#file-breadcrumbs").get_by_role(
            "button", name=directory.name
        )
    ).to_be_disabled()
    page.locator("#file-breadcrumbs").get_by_role(
        "button", name="Workspace"
    ).click()
    expect(page.locator(".wg-files-list")).to_contain_text(directory.name)
    expect(
        page.locator("#file-breadcrumbs").get_by_role(
            "button", name="Workspace"
        )
    ).to_be_disabled()

    # Independent synthetic roots exercise exact source order, selection,
    # disabled/offline, safe text, case-sensitive paths and stable React keys.
    page.evaluate("""() => {
      const locations=document.createElement('nav');locations.id='react-files-locations-contract';
      const crumbs=document.createElement('nav');crumbs.id='react-files-breadcrumbs-contract';
      crumbs.className='file-breadcrumbs';
      document.getElementById('app-main').append(locations,crumbs);
      window.__fileNavClicks={executor:[],path:[]};
      window.__fileNavView=window.WorkgateReactFilesNavigation.mount(locations,crumbs,{
        executor:id=>window.__fileNavClicks.executor.push(id),
        path:path=>window.__fileNavClicks.path.push(path)
      });
      window.__fileLocations=[
        {id:'ex1',name:'Alpha',state:'online'},
        {id:'ex2',name:'<img src=x onerror="window.__fileNavXss=1">',state:'offline'},
        {id:'ex3',name:'Beta',state:'online'}
      ];
      window.__fileCrumbs=[
        {path:'.',label:'Workspace'},
        {path:'alpha',label:'alpha'},
        {path:'alpha/docs',label:'docs'},
        {path:'alpha/docs/notes',label:'notes'}
      ];
      window.__fileNavXss=0;
      window.__fileNavView.renderLocations(window.__fileLocations,'ex1');
      window.__fileNavView.renderBreadcrumbs(window.__fileCrumbs,'alpha/docs/notes',false);
    }""")
    locations = page.locator("#react-files-locations-contract")
    crumbs = page.locator("#react-files-breadcrumbs-contract")
    expect(locations.locator(".file-location-row")).to_have_count(3)
    expect(locations.locator('[data-executor-id="ex1"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(locations.locator('[data-executor-id="ex2"]')).to_be_disabled()
    expect(
        locations.locator('[data-executor-id="ex2"] .file-location-dot')
    ).not_to_have_class("file-location-dot online")
    assert locations.locator("img").count() == 0
    assert page.evaluate("window.__fileNavXss") == 0
    expect(crumbs.locator("button")).to_have_text(
        ["Workspace", "alpha", "docs", "notes"]
    )
    expect(crumbs.locator("button").last).to_be_disabled()
    locations.locator('[data-executor-id="ex3"]').click()
    crumbs.get_by_role("button", name="alpha").click()
    assert page.evaluate("window.__fileNavClicks") == {
        "executor": ["ex3"],
        "path": ["alpha"],
    }

    # Exact cloned snapshots cause zero DOM mutations, retaining focused node
    # and actual scroll; background refresh must not re-create whole rails.
    crumbs.evaluate(
        "el=>{el.style.display='block';el.style.height='24px';el.style.minHeight='0';el.style.overflowY='scroll';el.scrollTop=18;}"
    )
    crumb = crumbs.get_by_role("button", name="alpha")
    crumb.focus()
    expect(crumb).to_be_focused()
    scroll_before = crumbs.evaluate("el=>el.scrollTop")
    assert scroll_before > 0
    page.evaluate("""() => {
      window.__savedFileCrumb=document.activeElement;
      window.__savedFileLocation=document.querySelector('#react-files-locations-contract [data-executor-id="ex3"]');
      window.__fileNavMutations=0;
      for(const id of ['react-files-locations-contract','react-files-breadcrumbs-contract']){
        new MutationObserver(records=>{
          for(const r of records) window.__fileNavMutations +=
            r.type==='attributes'?1:r.addedNodes.length+r.removedNodes.length;
        }).observe(document.getElementById(id),{subtree:true,attributes:true,childList:true});
      }
      window.__fileNavView.renderLocations(window.__fileLocations.map(row=>({...row})),'ex1');
      window.__fileNavView.renderBreadcrumbs(window.__fileCrumbs.map(row=>({...row})),'alpha/docs/notes',false);
    }""")
    assert page.evaluate("window.__fileNavMutations") == 0
    expect(crumb).to_be_focused()
    assert crumb.evaluate("el=>el === window.__savedFileCrumb")
    assert crumbs.evaluate("el=>el.scrollTop") == scroll_before
    assert locations.locator('[data-executor-id="ex3"]').evaluate(
        "el=>el === window.__savedFileLocation"
    )

    page.evaluate("""() => {
      window.__fileNavView.renderLocations([
        {...window.__fileLocations[0],state:'offline'},
        {...window.__fileLocations[1],state:'online',name:'Renamed'},
        window.__fileLocations[2]
      ],'ex2');
      window.__fileNavView.renderBreadcrumbs(window.__fileCrumbs,'alpha/docs/notes',true);
    }""")
    expect(locations.locator('[data-executor-id="ex1"]')).to_be_disabled()
    expect(locations.locator('[data-executor-id="ex2"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(crumbs.locator("button:not(:disabled)")).to_have_count(0)
    assert crumb.evaluate("el=>el === window.__savedFileCrumb")
    page.evaluate("""() => {
      window.__fileNavView.renderBreadcrumbs([{path:'/',label:'/'},
        {path:'/absolute',label:'absolute'}],'/absolute',false);
      window.__fileNavView.renderLocations([], '');
    }""")
    expect(crumbs.locator("button")).to_have_text(["/", "absolute"])
    expect(crumbs.locator("button").last).to_be_disabled()
    expect(locations.locator("button")).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
