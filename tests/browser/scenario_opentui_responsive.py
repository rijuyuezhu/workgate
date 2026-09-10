import time

from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness


def _assert_no_horizontal_overflow(harness: BrowserHarness) -> None:
    metrics = harness.page.evaluate(
        """
        () => ({
          body: document.body.scrollWidth,
          document: document.documentElement.scrollWidth,
          viewport: window.innerWidth,
        })
        """
    )
    assert metrics["body"] <= metrics["viewport"] + 1
    assert metrics["document"] <= metrics["viewport"] + 1


def _opentui_resize_events(harness: BrowserHarness, start: int) -> list[str]:
    return [
        event
        for event in harness.websocket_events[start:]
        if event.startswith("sent ws://")
        and "/ui/ws/opentui" in event
        and '{"type":"resize"' in event
    ]


def _wait_opentui_resize(harness: BrowserHarness, start: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _opentui_resize_events(harness, start):
            return
        harness.page.wait_for_timeout(50)
    raise AssertionError("OpenTUI route did not send a visible resize")


def run_opentui_responsive(harness: BrowserHarness) -> None:
    page = harness.page

    harness.navigate("console")
    expect(page.locator("#opentui-panel")).to_be_visible()
    page.locator("#opentui-start").click()
    expect(page.locator("#opentui-state")).to_have_text(
        "OpenTUI running", timeout=20_000
    )
    expect(page.locator("#opentui-terminal .xterm")).to_be_visible()
    visible_resize_start = len(harness.websocket_events)
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    _wait_opentui_resize(harness, visible_resize_start)

    hidden_resize_start = len(harness.websocket_events)
    harness.navigate("overview")
    page.wait_for_timeout(250)
    assert not _opentui_resize_events(harness, hidden_resize_start)

    visible_resize_start = len(harness.websocket_events)
    harness.navigate("console")
    _wait_opentui_resize(harness, visible_resize_start)
    page.get_by_label("OpenTUI shortcut keys").get_by_role(
        "button", name="Tab", exact=True
    ).click()
    page.set_viewport_size({"width": 1024, "height": 760})
    _assert_no_horizontal_overflow(harness)
    page.locator("#opentui-stop").click()
    expect(page.locator("#opentui-state")).to_have_text("Stopped")

    harness.opentui_crash_marker.write_text("crash once\n", encoding="utf-8")
    page.locator("#opentui-start").click()
    expect(page.locator("#opentui-state")).to_have_text(
        "OpenTUI exited; reconnecting", timeout=10_000
    )
    expect(page.locator("#opentui-state")).to_have_text(
        "OpenTUI running", timeout=20_000
    )
    page.locator("#opentui-stop").click()
    expect(page.locator("#opentui-state")).to_have_text("Stopped")

    page.set_viewport_size({"width": 390, "height": 844})
    _assert_no_horizontal_overflow(harness)
    views = {
        "overview": "#dashboard-panel",
        "executors": "#executors-panel",
        "sessions": "#session-panel",
        "terminals": "#terminal-panel",
        "files": "#file-panel",
        "audit": "#audit-panel",
        "console": "#opentui-panel",
    }
    for view, selector in views.items():
        harness.navigate(view)
        expect(page.locator(selector)).to_be_visible()
        for other_view, other_selector in views.items():
            if other_view != view:
                expect(page.locator(other_selector)).to_be_hidden()

    page.locator("#refresh").focus()
    expect(page.locator("#refresh")).to_be_focused()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement !== document.body")
    harness.navigate("files")
    page.locator("#file-path").focus()
    page.keyboard.type(".")
    expect(page.locator("#file-path")).to_be_focused()
    page.locator("#file-list").hover()
    page.mouse.wheel(0, 600)

    page.set_viewport_size({"width": 1440, "height": 1000})
    _assert_no_horizontal_overflow(harness)
