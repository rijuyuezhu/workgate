import secrets
import time

from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness


def _wait_terminal_output(
    harness: BrowserHarness,
    executor_id: str,
    shell_id: str,
    marker: str,
    *,
    websocket_event_start: int = 0,
) -> None:
    deadline = time.monotonic() + 15
    marker_seen = False
    while time.monotonic() < deadline:
        result = harness.api(
            "GET",
            f"/api/ui/terminals/read?executor_id={executor_id}&shell_id={shell_id}&lines=1000",
        )
        if result["status"] == 200:
            output = str(result["payload"]["data"].get("output") or "")
            marker_seen = marker_seen or marker in output
        websocket_seen = any(
            event.startswith("received ws://")
            for event in harness.websocket_events[websocket_event_start:]
        )
        if marker_seen and websocket_seen:
            return
        time.sleep(0.1)
    if marker_seen:
        raise AssertionError(
            "terminal output arrived without the expected WebSocket receive event"
        )
    raise AssertionError(f"terminal output did not contain {marker!r}")


def _wait_terminal_stream_marker(
    harness: BrowserHarness, marker: str, *, websocket_event_start: int
) -> None:
    """Check the live PTY bytes for a long output burst, not a tmux snapshot."""
    needle = marker.encode().hex()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if any(
            event.startswith("received ws://")
            and needle in event.rsplit(" ", 1)[-1]
            for event in harness.websocket_events[websocket_event_start:]
        ):
            return
        harness.page.wait_for_timeout(50)
    raise AssertionError(
        f"terminal WebSocket output did not contain {marker!r}"
    )


def _terminal_resize_events(harness: BrowserHarness, start: int) -> list[str]:
    return [
        event
        for event in harness.websocket_events[start:]
        if event.startswith("sent ws://")
        and "/stream/stream_" in event
        and '{"type":"resize"' in event
    ]


def _wait_terminal_resize(harness: BrowserHarness, start: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _terminal_resize_events(harness, start):
            return
        harness.page.wait_for_timeout(50)
    raise AssertionError(
        "terminal StreamHub route did not send a visible resize"
    )


def _start_terminal(harness: BrowserHarness, name: str) -> str:
    page = harness.page
    page.locator("#terminal-name").fill(name)
    page.locator("#terminal-start-form").get_by_role(
        "button", name="New terminal"
    ).click()
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    executor_id = page.locator("#terminal-executor").input_value()
    inventory = harness.api(
        "GET",
        f"/api/ui/terminals?executor_id={executor_id}",
    )
    assert inventory["status"] == 200
    shells = inventory["payload"]["data"]["shells"]
    match = next(item for item in shells if item.get("shell_id") == name)
    shell_id = str(match["shell_id"])
    harness.track_terminal(executor_id, shell_id)
    return shell_id


def _send_terminal(
    harness: BrowserHarness,
    executor_id: str,
    shell_id: str,
    command: str,
    marker: str,
) -> None:
    assert marker not in command, (
        "marker must prove shell output, not terminal echo"
    )
    page = harness.page
    expect(page.locator("#terminal-input")).to_be_enabled()
    websocket_event_start = len(harness.websocket_events)
    page.locator("#terminal-input").fill(command)
    page.locator("#terminal-input-form").get_by_role(
        "button", name="Send"
    ).click()
    _wait_terminal_output(
        harness,
        executor_id,
        shell_id,
        marker,
        websocket_event_start=websocket_event_start,
    )


def run_terminals(harness: BrowserHarness) -> None:
    page = harness.page
    suffix = secrets.token_hex(4)
    harness.navigate("terminals")

    executor_id = harness.executor_id
    executor_shell = _start_terminal(harness, f"browser-executor-{suffix}")
    expect(page.locator('.terminal-session[aria-current="true"]')).to_have_css(
        "background-color", "rgb(46, 54, 84)"
    )
    expect(page.locator('.terminal-session[aria-current="true"]')).to_have_css(
        "color", "rgb(255, 255, 255)"
    )
    _send_terminal(
        harness,
        executor_id,
        executor_shell,
        "printf 'executor-terminal-%s\\n' e2e",
        "executor-terminal-e2e",
    )
    # Raw xterm has keyboard focus after attach; text enters the PTY without
    # going through the optional whole-command helper.
    expect(page.locator("#terminal-xterm textarea")).to_be_focused()
    raw_marker_start = len(harness.websocket_events)
    page.keyboard.type("printf 'raw-%s\\n' typing", delay=5)
    page.keyboard.press("Enter")
    _wait_terminal_output(
        harness,
        executor_id,
        executor_shell,
        "raw-typing",
        websocket_event_start=raw_marker_start,
    )

    # Copy is a no-op with clear feedback when no text is selected.
    page.locator("#terminal-copy").click()
    expect(page.locator("#terminal-feedback")).to_contain_text("Select text")
    page.evaluate(r"""() => {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {
          readText: async () => "printf 'paste-%s\\n' clipboard",
          writeText: async (value) => { window.__terminalCopied = value; },
        },
      });
    }""")
    paste_start = len(harness.websocket_events)
    page.locator("#terminal-paste").click()
    expect(page.locator("#terminal-feedback")).to_contain_text("Pasted")
    page.keyboard.press("Enter")
    _wait_terminal_output(
        harness,
        executor_id,
        executor_shell,
        "paste-clipboard",
        websocket_event_start=paste_start,
    )
    expect(page.locator("#terminal-feedback")).to_contain_text("Pasted")

    # An explicit reconnect must reattach this PTY, not create a new one.
    reconnect_start = len(harness.websocket_events)
    page.locator("#terminal-reconnect").click()
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    expect(page.locator("#terminal-xterm textarea")).to_be_focused()
    _send_terminal(
        harness,
        executor_id,
        executor_shell,
        "printf 'reconnect-%s\\n' same-shell",
        "reconnect-same-shell",
    )
    assert len(harness.websocket_events) > reconnect_start

    # Container size changes can happen without window resize (sidebars,
    # narrow layouts); observe and send PTY resize on the same socket.
    container_resize_start = len(harness.websocket_events)
    page.locator("#terminal-xterm").evaluate(
        "el => { el.style.width = '390px'; }"
    )
    _wait_terminal_resize(harness, container_resize_start)
    page.locator("#terminal-xterm").evaluate("el => { el.style.width = ''; }")

    initial_resize_start = len(harness.websocket_events)
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    _wait_terminal_resize(harness, initial_resize_start)

    harness.navigate("files")
    # Count only events emitted after the view has actually become hidden;
    # an earlier terminal resize can legitimately complete during navigation.
    hidden_resize_start = len(harness.websocket_events)
    page.wait_for_timeout(250)
    assert not _terminal_resize_events(harness, hidden_resize_start)

    visible_resize_start = len(harness.websocket_events)
    harness.navigate("terminals")
    _wait_terminal_resize(harness, visible_resize_start)
    expect(page.locator("#terminal-xterm textarea")).to_be_focused()
    # Re-selecting the already attached session must return focus to xterm.
    page.locator(
        f'#terminal-list .terminal-session[title*="{executor_shell}"]'
    ).click()
    expect(page.locator("#terminal-xterm textarea")).to_be_focused()
    # Narrow browser views keep the terminal usable without horizontal overflow.
    page.set_viewport_size({"width": 390, "height": 780})
    expect(page.locator("#terminal-xterm .xterm-screen")).to_be_visible()
    terminal_bounds = page.locator("#terminal-xterm").bounding_box()
    assert (
        terminal_bounds
        and terminal_bounds["x"] + terminal_bounds["width"] <= 391
    )
    page.set_viewport_size({"width": 1280, "height": 720})

    # Output across several viewport heights must reach the live xterm stream.
    # Very large high-speed bursts are a separate terminal-stream stress case.
    scroll_start = len(harness.websocket_events)
    page.locator("#terminal-input").fill(
        "printf '%s\\n' {1..50}; sleep 0.1; printf 'scroll-%s\\n' complete"
    )
    page.locator("#terminal-input-form").get_by_role(
        "button", name="Send"
    ).click()
    _wait_terminal_stream_marker(
        harness, "scroll-complete", websocket_event_start=scroll_start
    )
    viewport = page.locator("#terminal-xterm .xterm-viewport")
    expect(viewport).to_be_visible()
    screen = page.locator("#terminal-xterm .xterm-screen")
    expect(screen).to_be_visible()
    before_scroll = len(harness.websocket_events)
    screen.hover()
    page.mouse.wheel(0, -1600)
    deadline = time.monotonic() + 5
    sent: list[str] = []
    while time.monotonic() < deadline:
        sent = [
            event
            for event in harness.websocket_events[before_scroll:]
            if event.startswith("sent ws://")
        ]
        if sent:
            break
        time.sleep(0.05)
    assert sent
    page.keyboard.press("End")

    reload_websocket_start = len(harness.websocket_events)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    session_button = page.locator(
        f'#terminal-list .terminal-session[title*="{executor_shell}"]'
    )
    expect(session_button).to_be_visible()
    session_button.click()
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    expect(page.locator("#terminal-xterm .xterm")).to_be_visible()
    # A browser reload reattaches the same shell. The preceding scroll can
    # leave tmux in copy mode, so do not assume typed text executes as a shell
    # command until that mode is explicitly exited. Earlier reconnect tests
    # independently verify fresh command execution on this shell.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(
            event.startswith("received ws://")
            for event in harness.websocket_events[reload_websocket_start:]
        ):
            break
        page.wait_for_timeout(50)
    else:
        raise AssertionError("reloaded terminal did not receive PTY output")
    harness.navigate("terminals")
    page.locator(
        f'#terminal-list .terminal-session[title*="{executor_shell}"]'
    ).click()
    page.locator("#terminal-kill").click()
    expect(page.locator("#terminal-state")).to_have_text("0 sessions")
