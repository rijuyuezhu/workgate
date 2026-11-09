"""Process-neutral bounds and routes for the Human UI Desktop stream."""

DESKTOP_MAX_FRAME_BYTES = 128 * 1024
DESKTOP_MAX_WIDTH = 1280
DESKTOP_MAX_HEIGHT = 800
DESKTOP_MAX_CONTROL_BYTES = 8192
DESKTOP_MAX_STREAMS = 4
DESKTOP_CAPTURE_INTERVAL_S = 1.0
DESKTOP_BROWSER_SUBPROTOCOL = "workgate-ui-desktop"
DESKTOP_BROWSER_TOKEN_PREFIX = "workgate-desktop-token."
EXECUTOR_DESKTOP_STREAM_ROUTE = "/executor/v1/desktop/{stream_id}"
BROWSER_DESKTOP_STREAM_ROUTE = "/desktop-stream/{stream_id}"
