"""Desktop WebSocket data plane is isolated from terminal and bearer reuse."""

from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.control.test_control_stream_routes import _Transport
from workgate.control.http.stream_routes import (
    desktop_stream_routes,
    terminal_stream_routes,
)
from workgate.control.streams import ControlStreamHub
from workgate.protocol.desktop import (
    DESKTOP_BROWSER_SUBPROTOCOL,
    DESKTOP_BROWSER_TOKEN_PREFIX,
    DESKTOP_MAX_FRAME_BYTES,
)


def test_desktop_stream_bounded_relay_and_separate_terminal_tokens():
    transport = _Transport({"executor-key": "ex1"})
    desktop = ControlStreamHub(
        max_streams=1, idle_timeout_s=0, max_frame_bytes=DESKTOP_MAX_FRAME_BYTES
    )
    terminal = ControlStreamHub(max_streams=1, idle_timeout_s=0)
    app = Starlette(
        routes=[
            *desktop_stream_routes(cast(Any, transport), desktop),
            *terminal_stream_routes(cast(Any, transport), terminal),
        ]
    )
    with TestClient(app) as client:
        assert client.portal is not None
        grant = client.portal.call(desktop.create, "ex1")
        protocols = [
            DESKTOP_BROWSER_SUBPROTOCOL,
            f"{DESKTOP_BROWSER_TOKEN_PREFIX}{grant.browser_token}",
        ]
        with client.websocket_connect(
            f"/executor/v1/desktop/{grant.stream_id}",
            headers={"Authorization": "Bearer executor-key"},
        ) as executor:
            assert executor.receive_json() == {
                "type": "stream-accepted",
                "stream_id": grant.stream_id,
            }
            with client.websocket_connect(
                f"/desktop-stream/{grant.stream_id}", subprotocols=protocols
            ) as browser:
                executor.send_json({"type": "frame", "state_id": "one"})
                assert browser.receive_json()["type"] == "frame"
                jpeg = (
                    b"\xff\xd8"
                    + b"x" * (DESKTOP_MAX_FRAME_BYTES - 4)
                    + b"\xff\xd9"
                )
                executor.send_bytes(jpeg)
                assert browser.receive_bytes() == jpeg
                browser.send_json({"type": "refresh"})
                assert executor.receive_json() == {"type": "refresh"}
                with (
                    pytest.raises(WebSocketDisconnect) as wrong_route,
                    client.websocket_connect(
                        f"/stream/{grant.stream_id}", subprotocols=protocols
                    ),
                ):
                    pass
                assert wrong_route.value.code == 4401
                with (
                    client.websocket_connect(
                        f"/desktop-stream/{grant.stream_id}",
                        subprotocols=protocols,
                    ) as reused,
                    pytest.raises(WebSocketDisconnect) as replay,
                ):
                    reused.receive_text()
                assert replay.value.code == 4401
    assert desktop.active_count() == 0
    assert terminal.active_count() == 0


def test_desktop_stream_rejects_wrong_executor_and_token_protocol():
    transport = _Transport({"key1": "ex1", "key2": "ex2"})
    hub = ControlStreamHub(
        max_streams=1, idle_timeout_s=0, max_frame_bytes=DESKTOP_MAX_FRAME_BYTES
    )
    with TestClient(
        Starlette(routes=desktop_stream_routes(cast(Any, transport), hub))
    ) as client:
        assert client.portal is not None
        grant = client.portal.call(hub.create, "ex1")
        with (
            pytest.raises(WebSocketDisconnect) as bad_executor,
            client.websocket_connect(
                f"/executor/v1/desktop/{grant.stream_id}",
                headers={"Authorization": "Bearer key2"},
            ),
        ):
            pass
        assert bad_executor.value.code == 4403
        with (
            pytest.raises(WebSocketDisconnect) as missing_protocol,
            client.websocket_connect(
                f"/desktop-stream/{grant.stream_id}",
                subprotocols=[
                    f"{DESKTOP_BROWSER_TOKEN_PREFIX}{grant.browser_token}"
                ],
            ),
        ):
            pass
        assert missing_protocol.value.code == 4400
        with client.websocket_connect(
            f"/desktop-stream/{grant.stream_id}",
            subprotocols=[
                DESKTOP_BROWSER_SUBPROTOCOL,
                f"{DESKTOP_BROWSER_TOKEN_PREFIX}{grant.browser_token}",
            ],
        ) as browser:
            assert browser.accepted_subprotocol == DESKTOP_BROWSER_SUBPROTOCOL
            browser.close()
