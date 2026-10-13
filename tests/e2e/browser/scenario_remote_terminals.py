"""Two independently paired executor runtimes sharing a Control StreamHub."""

from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness
from tests.e2e.browser.scenario_terminals import _send_terminal, _start_terminal


def run_remote_terminals(harness: BrowserHarness) -> None:
    remote_id = harness.remote_executor_id
    assert remote_id is not None and remote_id != harness.executor_id
    page = harness.page
    expect(page.locator("#executor-target-online")).to_have_text(
        "2", timeout=20_000
    )
    harness.navigate("terminals")

    page.locator("#terminal-executor").select_option(remote_id)
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
    page.locator("#terminal-executor").select_option(harness.executor_id)
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
    page.locator("#terminal-executor").select_option(remote_id)
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
