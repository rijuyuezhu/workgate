"""Two independently paired executor runtimes sharing a Control StreamHub."""

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness
from tests.e2e.browser.scenario_terminals import (
    _send_terminal,
    _start_terminal,
    _wait_terminal_stream_marker,
)


def run_remote_terminals(harness: BrowserHarness) -> None:
    remote_id = harness.remote_executor_id
    assert remote_id is not None and remote_id != harness.executor_id
    page = harness.page
    expect(page.locator("#executor-target-online")).to_have_text(
        "2", timeout=20_000
    )
    harness.navigate("terminals")

    page.locator(
        f'#terminal-executor-locations button[data-executor-id="{remote_id}"]'
    ).click()
    expect(page.locator("#terminal-executor")).to_have_value(remote_id)
    remote_shell = _start_terminal(harness, "remote-browser-shell")
    _send_terminal(
        harness,
        remote_id,
        remote_shell,
        "cat remote-marker.txt",
        "running-on-remote-executor",
    )

    # A new shell on the colocated executor must remain on A, not B.
    page.locator(
        f'#terminal-executor-locations button[data-executor-id="{harness.executor_id}"]'
    ).click()
    expect(page.locator("#terminal-executor")).to_have_value(
        harness.executor_id
    )
    local_shell = _start_terminal(harness, "local-browser-shell")
    _send_terminal(
        harness,
        harness.executor_id,
        local_shell,
        "cat notes.txt",
        "executor browser fixture",
    )

    # Re-enter remote B and reattach its existing tmux session.
    page.locator(
        f'#terminal-executor-locations button[data-executor-id="{remote_id}"]'
    ).click()
    expect(page.locator("#terminal-executor")).to_have_value(remote_id)
    remote_entry = page.locator(
        f'#terminal-list .terminal-session[title*="{remote_shell}"]'
    )
    expect(remote_entry).to_be_visible()
    remote_entry.click()
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    _send_terminal(
        harness,
        remote_id,
        remote_shell,
        "cat remote-marker.txt",
        "running-on-remote-executor",
    )

    # A second logged-in browser can attach the *same* HP tmux session.
    # Both WebSockets stay active; tmux owns concurrent input and screen state.
    second_page = harness.context.new_page()
    try:
        second_page.goto(
            f"{harness.base_url}/ui", wait_until="domcontentloaded"
        )
        expect(second_page.locator("#connection-state")).to_have_text(
            "Connected"
        )
        second_page.locator('.nav-item[data-view="terminals"]').click()
        second_page.locator(
            f'#terminal-executor-locations button[data-executor-id="{remote_id}"]'
        ).click()
        second_entry = second_page.locator(
            f'#terminal-list .terminal-session[title*="{remote_shell}"]'
        )
        expect(second_entry).to_be_visible()
        second_entry.click()
        expect(second_page.locator("#terminal-state")).to_contain_text(
            "Connected"
        )
        expect(page.locator("#terminal-state")).to_contain_text("Connected")
        # Raw WebSocket PTY input from the second client appears on the first.
        received_start = len(harness.websocket_events)
        expect(second_page.locator("#terminal-xterm textarea")).to_be_focused()
        second_page.keyboard.type("printf 'dual-%s\\n' client", delay=5)
        second_page.keyboard.press("Enter")
        _wait_terminal_stream_marker(
            harness, "dual-client", websocket_event_start=received_start
        )
    finally:
        second_page.close()

    # Closing one tab must not detach the first client's PTY.
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    _send_terminal(
        harness,
        remote_id,
        remote_shell,
        "cat remote-marker.txt",
        "running-on-remote-executor",
    )


def run_terminal_switch_races(harness: BrowserHarness) -> None:
    """Last click wins even when terminal lists and attach grants arrive late."""
    remote_id = harness.remote_executor_id
    assert remote_id is not None
    local_id = harness.executor_id
    page = harness.page
    harness.navigate("terminals")
    remote_button = page.locator(
        f'#terminal-executor-locations button[data-executor-id="{remote_id}"]'
    )
    local_button = page.locator(
        f'#terminal-executor-locations button[data-executor-id="{local_id}"]'
    )
    expect(remote_button).to_have_attribute("aria-current", "true")

    # Delay all three GET responses and complete them out of order. Fast
    # location clicks must not be ignored just because a list is loading.
    page.evaluate("""() => {
      const original = window.fetch;
      window.__terminalFetch = original;
      window.__terminalHeldLists = [];
      window.fetch = async (...args) => {
        const response = await original(...args);
        if (String(args[0]).includes('/api/ui/terminals?')) {
          return new Promise(resolve => {
            window.__terminalHeldLists.push(() => resolve(response));
          });
        }
        return response;
      };
    }""")
    try:
        page.evaluate(
            """([a, b]) => {
          const row = id => document.querySelector(
            `#terminal-executor-locations [data-executor-id="${id}"]`
          );
          row(a).click(); row(b).click(); row(a).click();
        }""",
            [local_id, remote_id],
        )
        expect(page.locator("#terminal-executor")).to_have_value(local_id)
        expect(
            page.locator('#terminal-executor-locations [aria-current="true"]')
        ).to_have_count(1)
        page.wait_for_function("window.__terminalHeldLists.length === 3")
        page.evaluate("""() => {
          window.__terminalHeldLists[1]();
          window.__terminalHeldLists[0]();
          window.__terminalHeldLists[2]();
        }""")
        expect(page.locator("#terminal-list")).to_contain_text(
            "local-browser-shell"
        )
        expect(page.locator("#terminal-list")).not_to_contain_text(
            "remote-browser-shell"
        )
        expect(local_button).to_have_attribute("aria-current", "true")
        expect(remote_button).to_have_attribute("aria-current", "false")
        local_button.hover()
        expect(local_button).to_have_css(
            "background-color", "rgb(238, 236, 255)"
        )
        remote_button.hover()
        expect(remote_button).to_have_css(
            "background-color", "rgb(238, 241, 246)"
        )
        remote_button.focus()
        expect(remote_button).to_have_css("outline-style", "solid")
        expect(
            page.locator('#terminal-executor-locations [aria-current="true"]')
        ).to_have_count(1)
    finally:
        page.evaluate("""() => {
          window.fetch = window.__terminalFetch;
          delete window.__terminalFetch;
          delete window.__terminalHeldLists;
        }""")

    # A stale attach grant arriving after switching executor must not activate
    # a socket for the previous shell. A fresh attach must still work.
    page.evaluate("""() => {
      const original = window.fetch;
      window.__terminalFetch = original;
      window.__terminalHeldAttach = [];
      window.fetch = async (...args) => {
        const response = await original(...args);
        if (String(args[0]).includes('/api/ui/terminals/attach')) {
          return new Promise(resolve => {
            window.__terminalHeldAttach.push(() => resolve(response));
          });
        }
        return response;
      };
    }""")
    try:
        page.locator(
            '#terminal-list .terminal-session[title*="local-browser-shell"]'
        ).click()
        page.wait_for_function("window.__terminalHeldAttach.length === 1")
        remote_button.click()
        expect(page.locator("#terminal-executor")).to_have_value(remote_id)
        page.evaluate("window.__terminalHeldAttach[0]()")
        expect(remote_button).to_have_attribute("aria-current", "true")
        expect(
            page.locator(
                '#terminal-list .terminal-session[title*="remote-browser-shell"]'
            )
        ).to_be_visible()
    finally:
        page.evaluate("""() => {
          window.fetch = window.__terminalFetch;
          delete window.__terminalFetch;
          delete window.__terminalHeldAttach;
        }""")

    remote_entry = page.locator(
        '#terminal-list .terminal-session[title*="remote-browser-shell"]'
    )
    remote_entry.click()
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    # Reattach the same tmux client immediately; #316 allows concurrent tmux
    # clients and retiring the old WebSocket must not cause a busy state.
    page.locator("#terminal-reconnect").click()
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    _send_terminal(
        harness,
        remote_id,
        "remote-browser-shell",
        "cat remote-marker.txt",
        "running-on-remote-executor",
    )
