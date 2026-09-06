"""Small authenticated HTTP client for executor protocol v1."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from ..protocol.errors import (
    ProtocolError,
    ProtocolErrorCode,
    ProtocolErrorResponse,
)
from ..protocol.executor import (
    EXECUTOR_HEARTBEAT_PATH,
    EXECUTOR_HELLO_PATH,
    EXECUTOR_POLL_PATH,
    EXECUTOR_RESULT_PATH,
    ExecutorCommand,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
)
from .profile import ExecutorProfile

_RETRYABLE_STATUS_CODES = {408, 425, 429}
_OWNER_ACTION_CODES = {
    ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
    ProtocolErrorCode.EXECUTOR_REVOKED,
    ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
}


@dataclass(frozen=True)
class ExecutorControlError(RuntimeError):
    """One bounded control request failure without exposing the bearer."""

    message: str
    status_code: int | None = None
    protocol_error: ProtocolError | None = None

    def __str__(self) -> str:
        return self.message

    @property
    def code(self) -> ProtocolErrorCode | None:
        return self.protocol_error.code if self.protocol_error else None

    @property
    def retryable(self) -> bool:
        if self.status_code is None:
            return True
        return (
            self.status_code in _RETRYABLE_STATUS_CODES
            or self.status_code >= 500
        )

    @property
    def requires_owner_action(self) -> bool:
        return self.code in _OWNER_ACTION_CODES


class ExecutorControlClient:
    """Typed executor v1 HTTP calls using one persisted bearer profile."""

    def __init__(
        self,
        profile: ExecutorProfile,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._profile = profile
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=profile.control_url,
            headers={"Authorization": f"Bearer {profile.credential}"},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def hello(
        self, message: ExecutorHelloRequest
    ) -> ExecutorHelloResponse:
        response = await self._post(
            EXECUTOR_HELLO_PATH,
            json=message.model_dump(mode="json"),
            timeout=30.0,
        )
        try:
            return ExecutorHelloResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ExecutorControlError(
                "control returned an invalid hello response"
            ) from exc

    async def heartbeat(self) -> None:
        await self._post(EXECUTOR_HEARTBEAT_PATH, json={}, timeout=30.0)

    async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
        response = await self._post(
            EXECUTOR_POLL_PATH,
            json={},
            timeout=max(1.0, timeout_s) + 5.0,
        )
        if response.status_code == 204:
            return None
        try:
            return ExecutorCommand.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ExecutorControlError(
                "control returned an invalid command"
            ) from exc

    async def submit_result(self, result: ExecutorResult) -> None:
        await self._post(
            EXECUTOR_RESULT_PATH,
            json=result.model_dump(mode="json"),
            timeout=30.0,
        )

    async def _post(
        self,
        path: str,
        *,
        json: object,
        timeout: float,
    ) -> httpx.Response:
        try:
            response = await self._client.post(path, json=json, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ExecutorControlError(
                f"control request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code < 400:
            return response
        raise _response_error(response)


def _response_error(response: httpx.Response) -> ExecutorControlError:
    protocol_error: ProtocolError | None = None
    with contextlib.suppress(ValueError, ValidationError):
        protocol_error = ProtocolErrorResponse.model_validate(
            response.json()
        ).error
    if protocol_error is not None:
        message = (
            f"control rejected executor request: {protocol_error.code.value}"
        )
    else:
        message = f"control rejected executor request with HTTP {response.status_code}"
    return ExecutorControlError(
        message,
        status_code=response.status_code,
        protocol_error=protocol_error,
    )
