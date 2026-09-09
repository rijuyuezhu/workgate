import secrets
import time

from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness


def _wait_terminal_output(
    harness: BrowserHarness,
    machine: str,
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
            f"/api/ui/terminals/read?machine={machine}&shell_id={shell_id}&lines=1000",
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


def _terminal_resize_events(
    harness: BrowserHarness, shell_id: str, start: int
) -> list[str]:
    return [
        event
        for event in harness.websocket_events[start:]
        if event.startswith("sent ws://")
        and f"/ui/ws/terminals/{shell_id}?" in event
        and '{"type":"resize"' in event
    ]


def _wait_terminal_resize(
    harness: BrowserHarness, shell_id: str, start: int
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _terminal_resize_events(harness, shell_id, start):
            return
        harness.page.wait_for_timeout(50)
    raise AssertionError("terminal route did not send a visible resize")


def _start_terminal(harness: BrowserHarness, name: str) -> str:
    page = harness.page
    page.locator("#terminal-name").fill(name)
    page.locator("#terminal-start-form").get_by_role(
        "button", name="New terminal"
    ).click()
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    machine = page.locator("#terminal-machine").input_value()
    inventory = harness.api(
        "GET",
        f"/api/ui/terminals?machine={machine}",
    )
    assert inventory["status"] == 200
    shells = inventory["payload"]["data"]["shells"]
    match = next(item for item in shells if item.get("shell_id") == name)
    shell_id = str(match["shell_id"])
    harness.track_terminal(machine, shell_id)
    return shell_id


def _send_terminal(
    harness: BrowserHarness,
    machine: str,
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
        machine,
        shell_id,
        marker,
        websocket_event_start=websocket_event_start,
    )


def run_terminals_remote(harness: BrowserHarness) -> None:
    page = harness.page
    suffix = secrets.token_hex(4)
    harness.navigate("terminals")

    local_shell = _start_terminal(harness, f"browser-local-{suffix}")
    _send_terminal(
        harness,
        "local",
        local_shell,
        "printf 'local-terminal-%s\\n' e2e",
        "local-terminal-e2e",
    )
    initial_resize_start = len(harness.websocket_events)
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    _wait_terminal_resize(harness, local_shell, initial_resize_start)

    hidden_resize_start = len(harness.websocket_events)
    harness.navigate("files")
    page.wait_for_timeout(250)
    assert not _terminal_resize_events(
        harness, local_shell, hidden_resize_start
    )

    visible_resize_start = len(harness.websocket_events)
    harness.navigate("terminals")
    _wait_terminal_resize(harness, local_shell, visible_resize_start)
    resized = harness.api(
        "POST",
        "/api/ui/terminals/resize",
        body={
            "machine": "local",
            "shell_id": local_shell,
            "cols": 111,
            "rows": 37,
        },
    )
    assert resized["status"] == 200
    assert resized["payload"]["data"]["cols"] == 111

    _send_terminal(
        harness,
        "local",
        local_shell,
        "printf '%s\\n' {1..180}; printf 'scroll-%s\\n' complete",
        "scroll-complete",
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
        f'#terminal-list .terminal-session[title*="{local_shell}"]'
    )
    expect(session_button).to_be_visible()
    session_button.click()
    expect(page.locator("#terminal-state")).to_contain_text("Connected")
    expect(page.locator("#terminal-xterm .xterm")).to_be_visible()
    _wait_terminal_output(
        harness,
        "local",
        local_shell,
        "scroll-complete",
        websocket_event_start=reload_websocket_start,
    )
    # Legacy remote-worker enrollment UI was retired in PR5. Browser coverage
    # now stays on the local Human UI path until final executor-backed remote
    # file/session/terminal routing lands in the later migration PRs. The
    # legacy remote protocol remains covered by its non-browser E2E suite.
    harness.navigate("terminals")
    page.locator(
        f'#terminal-list .terminal-session[title*="{local_shell}"]'
    ).click()
    page.locator("#terminal-kill").click()
    expect(page.locator("#terminal-state")).to_contain_text(
        "0 session(s) · local"
    )
