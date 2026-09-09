from fastapi import FastAPI
from fastapi.testclient import TestClient

from workgate.control.http.errors import install_error_handlers
from workgate.tool_session import SessionTerminationRequestedError
from workgate.tools.local_handlers import UnknownLocalToolError


def test_control_http_error_handlers_preserve_session_and_unknown_tool_shapes() -> (
    None
):
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/terminating")
    async def terminating():
        raise SessionTerminationRequestedError("sess_stopping")

    @app.get("/unknown")
    async def unknown():
        raise UnknownLocalToolError("Unknown local tool: missing")

    client = TestClient(app, raise_server_exceptions=False)

    terminating_response = client.get("/terminating")
    assert terminating_response.status_code == 409
    assert terminating_response.json() == {
        "error": "session_termination_requested",
        "message": terminating_response.json()["message"],
        "session_id": "sess_stopping",
    }
    assert "sess_stopping" in terminating_response.json()["message"]

    unknown_response = client.get("/unknown")
    assert unknown_response.status_code == 404
    assert unknown_response.json() == {
        "error": "unknown_tool",
        "message": "Unknown local tool: missing",
    }
