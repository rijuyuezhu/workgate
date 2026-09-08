from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import workgate.remote.http as remote_http
import workgate.remote.service as remote_service


def test_legacy_remote_poll_rejects_invalid_json_before_manager_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Manager:
        async def poll(self, *_args: Any, **_kwargs: Any):
            raise AssertionError(
                "invalid JSON must not reach the remote manager"
            )

    monkeypatch.setattr(remote_http, "remote_manager", lambda: Manager())
    app = Starlette(
        routes=[Route("/poll", remote_http.poll_endpoint, methods=["POST"])]
    )

    response = TestClient(app).post(
        "/poll",
        content=b"{not-json",
        headers={"authorization": "Bearer worker-token"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "ValueError"
    assert body["message"] == "request body must be valid JSON"


@pytest.mark.asyncio
async def test_legacy_remote_service_preserves_invite_and_tool_call_manager_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Manager:
        async def create_invite(
            self, name: str | None, workdir: str | None, ttl_s: int | None
        ):
            return (name, workdir, ttl_s)

        async def call(
            self,
            machine: str,
            tool: str,
            args: dict[str, Any],
            timeout_s: int | None,
        ):
            return {
                "machine": machine,
                "tool": tool,
                "args": args,
                "timeout_s": timeout_s,
            }

    monkeypatch.setattr(remote_service, "remote_manager", lambda: Manager())

    invite = await remote_service.create_remote_invite("edge", "/work", 30)
    result = await remote_service.call_remote_worker_tool(
        "edge",
        "read",
        {"path": "README.md"},
        7,
    )

    assert invite == ("edge", "/work", 30)
    assert result == {
        "machine": "edge",
        "tool": "read",
        "args": {"path": "README.md"},
        "timeout_s": 7,
    }
