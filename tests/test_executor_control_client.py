from collections.abc import Callable

import httpx
import pytest

from workgate.executor.control_client import (
    ExecutorControlClient,
    ExecutorControlError,
)
from workgate.executor.profile import ExecutorProfile
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import (
    EXECUTOR_HEARTBEAT_PATH,
    EXECUTOR_HELLO_PATH,
    EXECUTOR_POLL_PATH,
    EXECUTOR_RESULT_PATH,
    ExecutorHelloRequest,
    ExecutorResult,
    ExecutorRuntimeSummary,
)
from workgate.protocol.ids import new_command_id, new_executor_id


def _profile() -> ExecutorProfile:
    return ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )


def _hello() -> ExecutorHelloRequest:
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        sessions=(),
        shells=(),
        jobs=(),
    )


def _client(
    profile: ExecutorProfile,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[ExecutorControlClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url=profile.control_url,
        headers={"Authorization": f"Bearer {profile.credential}"},
        transport=transport,
    )
    return ExecutorControlClient(profile, client=http), http


@pytest.mark.asyncio
async def test_executor_control_client_uses_v1_paths_and_persisted_bearer() -> (
    None
):
    profile = _profile()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["authorization"]))
        if request.url.path == EXECUTOR_HELLO_PATH:
            return httpx.Response(
                200,
                json={
                    "protocol_version": 1,
                    "heartbeat_interval_s": 10,
                    "offline_after_s": 30,
                    "poll_timeout_s": 25,
                },
            )
        if request.url.path == EXECUTOR_POLL_PATH:
            return httpx.Response(204)
        return httpx.Response(204)

    client, http = _client(profile, handler)
    try:
        policy = await client.hello(_hello())
        await client.heartbeat()
        command = await client.poll(timeout_s=policy.poll_timeout_s)
        await client.submit_result(
            ExecutorResult(id=new_command_id(), ok=True, result={"ok": True})
        )
    finally:
        await client.aclose()
        await http.aclose()

    assert command is None
    assert [path for path, _ in seen] == [
        EXECUTOR_HELLO_PATH,
        EXECUTOR_HEARTBEAT_PATH,
        EXECUTOR_POLL_PATH,
        EXECUTOR_RESULT_PATH,
    ]
    assert {auth for _, auth in seen} == {f"Bearer {profile.credential}"}


@pytest.mark.asyncio
async def test_executor_control_client_parses_terminal_protocol_error() -> None:
    profile = _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "executor_revoked",
                    "message": "executor credential is revoked",
                }
            },
        )

    client, http = _client(profile, handler)
    try:
        with pytest.raises(ExecutorControlError) as caught:
            await client.heartbeat()
    finally:
        await client.aclose()
        await http.aclose()

    assert caught.value.code is ProtocolErrorCode.EXECUTOR_REVOKED
    assert caught.value.requires_owner_action
    assert not caught.value.retryable
    assert profile.credential not in str(caught.value)


@pytest.mark.asyncio
async def test_executor_control_client_marks_server_error_retryable() -> None:
    profile = _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "temporarily unavailable"})

    client, http = _client(profile, handler)
    try:
        with pytest.raises(ExecutorControlError) as caught:
            await client.heartbeat()
    finally:
        await client.aclose()
        await http.aclose()

    assert caught.value.status_code == 503
    assert caught.value.retryable
    assert not caught.value.requires_owner_action
