from typing import Any, cast

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from workgate.control.http.stream_routes import terminal_stream_routes
from workgate.control.streams import ControlStreamHub
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.terminal import (
    TERMINAL_BROWSER_SUBPROTOCOL,
    TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX,
)


class _Transport:
    def __init__(self, credentials: dict[str, str]) -> None:
        self.credentials = credentials

    def authenticate_live_bearer(self, bearer: str) -> str:
        executor_id = self.credentials.get(bearer)
        if executor_id is None:
            from workgate.control.executor_transport import (
                ExecutorTransportError,
            )

            raise ExecutorTransportError(
                ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
                "Executor authentication failed",
            )
        return executor_id


def _app(hub: ControlStreamHub, transport: _Transport) -> Starlette:
    return Starlette(routes=terminal_stream_routes(cast(Any, transport), hub))


def test_terminal_stream_routes_pair_and_relay_bidirectionally() -> None:
    hub = ControlStreamHub(max_streams=2, idle_timeout_s=0)
    transport = _Transport({"exec-secret": "exec-1"})
    with TestClient(_app(hub, transport)) as client:
        assert client.portal is not None
        grant = client.portal.call(hub.create, "exec-1")
        browser_protocols = [
            TERMINAL_BROWSER_SUBPROTOCOL,
            f"{TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX}{grant.browser_token}",
        ]

        with client.websocket_connect(
            f"/executor/v1/streams/{grant.stream_id}",
            headers={"Authorization": "Bearer exec-secret"},
        ) as executor:
            assert executor.receive_json() == {
                "type": "stream-accepted",
                "stream_id": grant.stream_id,
            }
            with client.websocket_connect(
                f"/stream/{grant.stream_id}",
                subprotocols=browser_protocols,
            ) as browser:
                executor.send_json({"type": "ready", "mode": "pty"})
                assert browser.receive_json() == {
                    "type": "ready",
                    "mode": "pty",
                }

                browser.send_bytes(b"browser-input")
                assert executor.receive_bytes() == b"browser-input"

                executor.send_bytes(b"pty-output")
                assert browser.receive_bytes() == b"pty-output"

    assert hub.active_count() == 0


def test_terminal_stream_routes_reject_wrong_executor_and_reused_browser_token() -> (
    None
):
    hub = ControlStreamHub(max_streams=2, idle_timeout_s=0)
    transport = _Transport(
        {
            "exec-one-secret": "exec-1",
            "exec-two-secret": "exec-2",
        }
    )
    with TestClient(_app(hub, transport)) as client:
        assert client.portal is not None
        grant = client.portal.call(hub.create, "exec-1")
        protocols = [
            TERMINAL_BROWSER_SUBPROTOCOL,
            f"{TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX}{grant.browser_token}",
        ]

        with (
            pytest.raises(WebSocketDisconnect) as wrong_executor,
            client.websocket_connect(
                f"/executor/v1/streams/{grant.stream_id}",
                headers={"Authorization": "Bearer exec-two-secret"},
            ),
        ):
            pass
        assert wrong_executor.value.code == 4403

        with client.websocket_connect(
            f"/stream/{grant.stream_id}",
            subprotocols=protocols,
        ) as first_browser:
            with (
                client.websocket_connect(
                    f"/stream/{grant.stream_id}",
                    subprotocols=protocols,
                ) as reused_browser,
                pytest.raises(WebSocketDisconnect) as reused,
            ):
                reused_browser.receive_text()
            assert reused.value.code == 4401
            first_browser.close()


def test_terminal_stream_browser_requires_fixed_subprotocol() -> None:
    hub = ControlStreamHub(max_streams=1, idle_timeout_s=0)
    transport = _Transport({"exec-secret": "exec-1"})
    with TestClient(_app(hub, transport)) as client:
        assert client.portal is not None
        grant = client.portal.call(hub.create, "exec-1")
        with (
            pytest.raises(WebSocketDisconnect) as missing_protocol,
            client.websocket_connect(
                f"/stream/{grant.stream_id}",
                subprotocols=[
                    f"{TERMINAL_BROWSER_TOKEN_PROTOCOL_PREFIX}{grant.browser_token}"
                ],
            ),
        ):
            pass
        assert missing_protocol.value.code == 4400
