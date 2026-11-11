"""Real Terminal Executor/shell rail and keyed React focus/scope contracts."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness
from tests.e2e.browser.scenario_terminals import _start_terminal

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(165),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_react_terminal_rail_and_executor_locations(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    assert page.evaluate(
        "typeof window.WorkgateReactTerminalRail?.mount === 'function'"
    )
    harness.navigate("terminals")
    executor_id = harness.executor_id
    real_location = page.locator(
        f'#terminal-executor-locations [data-executor-id="{executor_id}"]'
    )
    expect(real_location).to_be_visible()
    expect(real_location).to_have_attribute("aria-current", "true")
    expect(real_location).to_be_enabled()
    shell_id = _start_terminal(harness, "react-rail-shell")
    real_shell = page.locator("#terminal-list .terminal-session").filter(
        has_text="react-rail-shell"
    )
    expect(real_shell).to_have_count(1)
    expect(real_shell).to_have_attribute("aria-current", "true")
    expect(real_shell.locator(".terminal-session-icon")).to_be_visible()
    expect(page.locator("#terminal-count")).to_contain_text("1")
    real_shell.click()
    expect(real_shell).to_have_attribute("aria-current", "true")
    expect(page.locator("#terminal-executor")).to_have_value(executor_id)
    page.locator("#terminal-search").fill("no-such-terminal")
    expect(page.locator("#terminal-list .empty-state")).to_have_text(
        "No matching sessions."
    )
    page.locator("#terminal-search").fill("")
    expect(real_shell).to_be_visible()
    expect(real_shell).to_have_attribute("aria-current", "true")
    page.locator("#terminal-kill").click()
    expect(
        page.locator("#terminal-list .terminal-session").filter(
            has_text=shell_id
        )
    ).to_have_count(0)

    # Independent mounts can test error/unavailable and cross-Executor ID
    # collisions without modifying the real Executor fleet or PTY transport.
    page.evaluate("""() => {
      const locations = document.createElement('nav');locations.id='react-terminal-locations-contract';
      locations.className='terminal-executor-locations';
      const shells = document.createElement('nav');shells.id='react-terminal-shells-contract';
      shells.className='terminal-list';
      document.getElementById('app-main').append(locations,shells);
      window.__terminalRailClicks = {executor:[],shell:[]};
      window.__terminalRailView=window.WorkgateReactTerminalRail.mount(locations,shells,{
        executor:id=>window.__terminalRailClicks.executor.push(id),
        shell:id=>window.__terminalRailClicks.shell.push(id),
      });
      window.__terminalLocations=[
        {id:'ex-1',name:'Alpha',state:'online'},
        {id:'ex-2',name:'<img src=x onerror="window.__terminalXss=1">',state:'online'},
        {id:'ex-3',name:'Offline',state:'offline'},
      ];
      window.__terminalShells={executorId:'ex-1',selectedShellId:'shell-b',emptyMessage:'No sessions',entries:[
        {id:'shell-a',name:'Shell A',title:'ex-1, shell-a, /tmp'},
        {id:'shell-b',name:'Shell B',title:'ex-1, shell-b, /home'},
        {id:'shell-c',name:'Shell C',title:'ex-1, shell-c, /data'}
      ]};
      window.__terminalXss=0;
      window.__terminalRailView.renderExecutors(window.__terminalLocations,'ex-1');
      window.__terminalRailView.renderSessions(window.__terminalShells);
    }""")
    locations = page.locator("#react-terminal-locations-contract")
    shells = page.locator("#react-terminal-shells-contract")
    expect(locations.locator("button")).to_have_count(3)
    expect(locations.locator('[data-executor-id="ex-3"]')).to_be_disabled()
    expect(
        locations.locator('[data-executor-id="ex-3"] .terminal-location-dot')
    ).not_to_have_class("terminal-location-dot online")
    expect(locations.locator('[data-executor-id="ex-1"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(shells.locator(".terminal-session")).to_have_count(3)
    expect(shells.locator(".terminal-session").nth(1)).to_have_attribute(
        "aria-current", "true"
    )
    expect(shells.locator(".terminal-session-icon")).to_have_count(3)
    assert locations.locator("img").count() == 0
    assert page.evaluate("window.__terminalXss") == 0
    locations.locator('[data-executor-id="ex-3"]').click(force=True)
    assert page.evaluate("window.__terminalRailClicks.executor") == []
    locations.locator('[data-executor-id="ex-2"]').click()
    shells.locator(".terminal-session").first.click()
    assert page.evaluate("window.__terminalRailClicks") == {
        "executor": ["ex-2"],
        "shell": ["shell-a"],
    }

    # Identical cloned snapshots must not mutate DOM, replace keyed buttons,
    # remove keyboard focus, or reset the actual rail scroll positions.
    shells.evaluate(
        "el => {el.style.display='block';el.style.height='40px';el.style.minHeight='0';el.style.overflowY='scroll';el.scrollTop=22;}"
    )
    selected = shells.locator(".terminal-session").nth(1)
    selected.focus()
    expect(selected).to_be_focused()
    scroll_before = shells.evaluate("el => el.scrollTop")
    assert scroll_before > 0
    page.evaluate("""() => {
      window.__savedShell=document.activeElement;
      window.__savedExecutor=document.querySelector('#react-terminal-locations-contract [data-executor-id="ex-2"]');
      window.__terminalRailChanges=0;
      for(const id of ['react-terminal-locations-contract','react-terminal-shells-contract']){
        new MutationObserver(records=>{
          for(const r of records) window.__terminalRailChanges+=
            r.type==='attributes'?1:r.addedNodes.length+r.removedNodes.length;
        }).observe(document.getElementById(id),{subtree:true,attributes:true,childList:true});
      }
      window.__terminalRailView.renderExecutors(window.__terminalLocations.map(row=>({...row})),'ex-1');
      window.__terminalRailView.renderSessions({...window.__terminalShells,
        entries:window.__terminalShells.entries.map(row=>({...row}))});
    }""")
    assert page.evaluate("window.__terminalRailChanges") == 0
    expect(selected).to_be_focused()
    assert selected.evaluate("el => el === window.__savedShell")
    assert shells.evaluate("el => el.scrollTop") == scroll_before
    assert locations.locator('[data-executor-id="ex-2"]').evaluate(
        "el => el === window.__savedExecutor"
    )

    # Status and names can update in place; reordered shells retain keys.
    page.evaluate("""() => {
      window.__terminalRailView.renderExecutors([
        {...window.__terminalLocations[0],state:'offline'},
        {...window.__terminalLocations[1],name:'Renamed Beta'},
        window.__terminalLocations[2]],'ex-2');
      window.__terminalRailView.renderSessions({...window.__terminalShells,
        entries:[window.__terminalShells.entries[2],window.__terminalShells.entries[1],window.__terminalShells.entries[0]]});
    }""")
    expect(locations.locator('[data-executor-id="ex-1"]')).to_be_disabled()
    expect(locations.locator('[data-executor-id="ex-2"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(locations.locator('[data-executor-id="ex-2"]')).to_contain_text(
        "Renamed Beta"
    )
    expect(selected).to_be_focused()
    assert selected.evaluate("el => el === window.__savedShell")

    # Same shell ID in a different Executor must remount the scoped row.
    page.evaluate("""() => window.__terminalRailView.renderSessions({
      executorId:'ex-2',selectedShellId:'',emptyMessage:'Not loaded',
      entries:[{id:'shell-b',name:'New scope shell',title:'ex-2, shell-b'}]
    })""")
    expect(shells.locator(".terminal-session")).to_have_count(1)
    expect(shells.locator(".terminal-session")).to_have_text("New scope shell")
    assert not shells.locator(".terminal-session").evaluate(
        "el => el === window.__savedShell"
    )
    page.evaluate("""() => {
      window.__terminalRailView.renderSessions({executorId:'ex-2',selectedShellId:'',entries:[],emptyMessage:'Unavailable'});
      window.__terminalRailView.renderExecutors([], '');
    }""")
    expect(shells.locator(".empty-state")).to_have_text("Unavailable")
    expect(locations.locator("button")).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
