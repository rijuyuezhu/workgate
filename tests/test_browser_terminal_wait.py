from typing import Any, cast

from tests.browser import scenario_terminals


class _DelayedWebSocketHarness:
    def __init__(self) -> None:
        self.websocket_events: list[str] = []
        self.calls = 0

    def api(self, _method: str, _path: str) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 2:
            self.websocket_events.append("received ws://terminal")
        return {
            "status": 200,
            "payload": {"data": {"output": "terminal-marker"}},
        }


def test_wait_terminal_output_allows_websocket_event_after_poll_output(
    monkeypatch,
) -> None:
    harness = _DelayedWebSocketHarness()
    monkeypatch.setattr(scenario_terminals.time, "sleep", lambda _value: None)

    scenario_terminals._wait_terminal_output(
        cast(Any, harness),
        "executor",
        "shell",
        "terminal-marker",
    )

    assert harness.calls == 2
