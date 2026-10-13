"""Two independently paired executor runtimes sharing a Control StreamHub."""

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness
from tests.e2e.browser.scenario_terminals import (
    _send_terminal,
    _start_terminal,
    _wait_terminal_output,
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
        # Input from the second client appears on the first client's stream.
        received_start = len(harness.websocket_events)
        second_page.locator("#terminal-input").fill(
            "printf 'dual-%s\\n' client"
        )
        second_page.locator("#terminal-input-form").get_by_role(
            "button", name="Send"
        ).click()
        _wait_terminal_output(
            harness,
            remote_id,
            remote_shell,
            "dual-client",
            websocket_event_start=received_start,
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
