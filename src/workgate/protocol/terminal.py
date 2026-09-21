"""Process-neutral Human UI terminal wire and dimension contracts."""

PERSISTENT_SHELL_MIN_COLUMNS = 20
PERSISTENT_SHELL_MAX_COLUMNS = 1600
PERSISTENT_SHELL_MIN_ROWS = 3
PERSISTENT_SHELL_MAX_ROWS = 500

TERMINAL_STREAM_MAX_FRAME_BYTES = 65_536
TERMINAL_BROWSER_SUBPROTOCOL = "workgate-ui-terminal"
TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX = "workgate-stream-token."
EXECUTOR_TERMINAL_STREAM_ROUTE = "/executor/v1/streams/{stream_id}"
BROWSER_TERMINAL_STREAM_ROUTE = "/stream/{stream_id}"
TERMINAL_BRIDGE_MAX_CHUNK_BYTES = TERMINAL_STREAM_MAX_FRAME_BYTES
TERMINAL_BRIDGE_BACKEND = "tmux-pty"
TERMINAL_BRIDGE_BACKENDS = frozenset({TERMINAL_BRIDGE_BACKEND, "conpty"})


class TerminalBridgeError(RuntimeError):
    """Base class for internal raw-terminal bridge failures."""


class TerminalBridgeUnsupportedError(TerminalBridgeError):
    """Raised when the current platform cannot create a raw PTY bridge."""


class TerminalBridgeBusyError(TerminalBridgeError):
    """Raised when a shell already has an exclusive Human UI raw attachment."""


class TerminalBridgeNotFoundError(TerminalBridgeError):
    """Raised when a bridge capability is unknown, expired, or already closed."""
