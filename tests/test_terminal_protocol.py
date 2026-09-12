import pytest

from workgate.ui.http.terminal_protocol import (
    UI_TERMINAL_INPUT_MAX_BYTES,
    TerminalCloseControl,
    TerminalInputControl,
    TerminalPingControl,
    TerminalResizeControl,
    parse_terminal_binary_input,
    parse_terminal_control,
    parse_terminal_websocket_request,
)


def test_terminal_websocket_request_defaults_are_stable():
    request = parse_terminal_websocket_request(
        executor_id="executor-a",
        shell_id="demo",
        mode=None,
        lines=None,
        cols=None,
        rows=None,
    )

    assert request.executor_id == "executor-a"
    assert request.shell_id == "demo"
    assert request.requested_mode == "snapshot"
    assert request.announce_mode is False
    assert request.lines == 1000
    assert request.cols == 120
    assert request.rows == 36


def test_terminal_websocket_request_preserves_explicit_mode_and_bounds():
    request = parse_terminal_websocket_request(
        executor_id="edge",
        shell_id="shared",
        mode="auto",
        lines="50",
        cols="100",
        rows="30",
    )

    assert request.executor_id == "edge"
    assert request.requested_mode == "auto"
    assert request.announce_mode is True
    assert (request.lines, request.cols, request.rows) == (50, 100, 30)

    with pytest.raises(ValueError, match="mode must be one of"):
        parse_terminal_websocket_request(
            executor_id="local",
            shell_id="demo",
            mode="raw",
            lines=None,
            cols=None,
            rows=None,
        )


def test_terminal_input_control_preserves_snapshot_enter_semantics():
    control = parse_terminal_control(
        '{"type":"input","data":"echo ok","enter":true}',
        active_mode="snapshot",
        current_cols=80,
        current_rows=24,
    )

    assert isinstance(control, TerminalInputControl)
    assert control.input_text == "echo ok"
    assert control.raw_input == b"echo ok"
    assert control.enter is True


def test_terminal_input_control_adds_carriage_return_for_pty_enter():
    control = parse_terminal_control(
        '{"type":"input","data":"echo raw","enter":true}',
        active_mode="pty",
        current_cols=80,
        current_rows=24,
    )

    assert isinstance(control, TerminalInputControl)
    assert control.raw_input == b"echo raw\r"


def test_terminal_resize_ping_and_close_controls_are_typed():
    resize = parse_terminal_control(
        '{"type":"resize","cols":120,"rows":36}',
        active_mode="pty",
        current_cols=80,
        current_rows=24,
    )
    ping = parse_terminal_control(
        '{"type":"ping"}',
        active_mode="snapshot",
        current_cols=80,
        current_rows=24,
    )
    close = parse_terminal_control(
        '{"type":"close"}',
        active_mode="snapshot",
        current_cols=80,
        current_rows=24,
    )

    assert resize == TerminalResizeControl(kind="resize", cols=120, rows=36)
    assert isinstance(ping, TerminalPingControl)
    assert isinstance(close, TerminalCloseControl)


def test_terminal_control_rejects_malformed_and_oversized_messages():
    with pytest.raises(ValueError, match="must be JSON"):
        parse_terminal_control(
            "not-json",
            active_mode="snapshot",
            current_cols=80,
            current_rows=24,
        )
    with pytest.raises(ValueError, match="must be an object"):
        parse_terminal_control(
            "[]",
            active_mode="snapshot",
            current_cols=80,
            current_rows=24,
        )
    with pytest.raises(ValueError, match="Unsupported terminal control"):
        parse_terminal_control(
            '{"type":"unknown"}',
            active_mode="snapshot",
            current_cols=80,
            current_rows=24,
        )
    with pytest.raises(ValueError, match="Terminal message is too large"):
        parse_terminal_control(
            "x" * (UI_TERMINAL_INPUT_MAX_BYTES + 1),
            active_mode="snapshot",
            current_cols=80,
            current_rows=24,
        )


def test_terminal_binary_input_enforces_wire_limit():
    payload = b"abc"
    assert parse_terminal_binary_input(payload) == payload
    with pytest.raises(ValueError, match="Terminal input is too large"):
        parse_terminal_binary_input(b"x" * (UI_TERMINAL_INPUT_MAX_BYTES + 1))
