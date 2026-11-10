"""Shared React read-only Session Skills/MCP groups preserve identity and scope."""

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


def test_react_session_integration_grouped_lists(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    page = harness.page
    assert page.evaluate(
        "typeof window.WorkgateReactSessionIntegrations?.mount === 'function'"
    )

    # The same viewer independently mounts MCPs and effective Skills. Keep the
    # test independent of the host Executor's installed Skills/plugins.
    page.evaluate("""() => {
      const mcp = document.createElement('div');
      mcp.id = 'react-mcp-contract'; mcp.className = 'session-integrations-list';
      const skills = document.createElement('div');
      skills.id = 'react-skills-contract'; skills.className = 'session-integrations-list';
      document.getElementById('app-main').append(mcp, skills);
      window.__integrationViews = window.WorkgateReactSessionIntegrations.mount(mcp, skills);
      window.__integrationMcp = [
        {heading:'Executor', entries:[
          {name:'shared',state:'Enabled'}, {name:'local-mcp',state:'Disabled'}]},
        {heading:'Control',entries:[
          {name:'shared',state:'Overridden'}, {name:'control-mcp',state:'Enabled'}]},
      ];
      window.__integrationSkills = [
        {heading:'Project',entries:[{name:'<img src=x onerror="window.__integrationXss=1">',state:'Effective'}]},
        {heading:'Executor',entries:[{name:'user-skill',state:'Effective'}]},
        {heading:'Executor (User Global)',entries:[{name:'global-skill',state:'Effective'}]},
        {heading:'Control',entries:[{name:'control-skill',state:'Effective'}]},
      ];
      window.__integrationXss=0;
      window.__integrationViews.render('mcp',window.__integrationMcp);
      window.__integrationViews.render('skills',window.__integrationSkills);
    }""")
    mcp = page.locator("#react-mcp-contract")
    skills = page.locator("#react-skills-contract")
    expect(mcp.locator(".executor-manage-group h4")).to_have_text(
        ["Executor", "Control"]
    )
    expect(skills.locator(".executor-manage-group h4")).to_have_text(
        ["Project", "Executor", "Executor (User Global)", "Control"]
    )
    expect(mcp.locator(".executor-manage-row")).to_have_count(4)
    expect(
        mcp.locator(".executor-manage-row").filter(has_text="shared")
    ).to_have_count(2)
    expect(mcp.locator(".executor-manage-pill")).to_have_text(
        ["Enabled", "Disabled", "Overridden", "Enabled"]
    )
    assert mcp.locator(".executor-manage-pill").evaluate_all(
        "items => items.map(item=>item.dataset.enabled)"
    ) == ["true", "false", "false", "true"]
    expect(skills).to_contain_text(
        '<img src=x onerror="window.__integrationXss=1">'
    )
    assert skills.locator("img").count() == 0
    assert page.evaluate("window.__integrationXss") == 0
    assert mcp.locator("button, input, select").count() == 0

    # Equal new object snapshots must preserve actual DOM, focus and scroll.
    mcp.evaluate(
        "el=>{el.style.display='block';el.style.height='35px';el.style.minHeight='0';el.style.overflowY='scroll';el.scrollTop=18;}"
    )
    retained = mcp.locator(".executor-manage-row").first
    retained.evaluate("el=>{el.tabIndex=0;el.focus({preventScroll:true});}")
    expect(retained).to_be_focused()
    scroll_before = mcp.evaluate("el=>el.scrollTop")
    assert scroll_before > 0, mcp.evaluate(
        "el=>({height:el.clientHeight,scrollHeight:el.scrollHeight,overflow:getComputedStyle(el).overflowY})"
    )
    page.evaluate("""() => {
      window.__retainedIntegrationRow=document.activeElement;
      window.__integrationChanges=0;
      for(const id of ['react-mcp-contract','react-skills-contract']) {
        new MutationObserver(records=>{
          for(const r of records) window.__integrationChanges+=
            r.type==='attributes'?1:r.addedNodes.length+r.removedNodes.length;
        }).observe(document.getElementById(id),{subtree:true,attributes:true,childList:true});
      }
      window.__integrationViews.render('mcp',window.__integrationMcp.map(group=>({
        heading:group.heading,entries:group.entries.map(entry=>({...entry}))
      })));
      window.__integrationViews.render('skills',window.__integrationSkills.map(group=>({
        heading:group.heading,entries:group.entries.map(entry=>({...entry}))
      })));
    }""")
    assert page.evaluate("window.__integrationChanges") == 0
    expect(retained).to_be_focused()
    assert retained.evaluate("el=>el === window.__retainedIntegrationRow")
    assert mcp.evaluate("el=>el.scrollTop") == scroll_before

    # Updated enabled state and simultaneous scope isolation retain keyed rows.
    page.evaluate("""() => window.__integrationViews.render('mcp',[
      {...window.__integrationMcp[0],entries:[
        {...window.__integrationMcp[0].entries[0],state:'Disabled'},
        window.__integrationMcp[0].entries[1]
      ]},window.__integrationMcp[1]])""")
    expect(mcp.locator(".executor-manage-pill").first).to_have_text("Disabled")
    expect(mcp.locator(".executor-manage-pill").first).to_have_attribute(
        "data-enabled", "false"
    )
    assert retained.evaluate("el=>el === window.__retainedIntegrationRow")
    expect(skills.locator(".executor-manage-row")).to_have_count(4)

    page.evaluate("() => window.__integrationViews.render('skills',[])")
    expect(skills.locator(".executor-manage-row")).to_have_count(0)
    expect(mcp.locator(".executor-manage-row")).to_have_count(4)
    page.evaluate("() => window.__integrationViews.render('mcp',[])")
    expect(mcp.locator(".executor-manage-row")).to_have_count(0)
    assert page.evaluate("window.__integrationXss") == 0
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 2"
    )
    harness.assert_clean_browser()
