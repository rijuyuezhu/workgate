"""Small authenticated HTTP client for executor protocol v1."""

import contextlib
from dataclasses import dataclass, replace

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
    EXECUTOR_VALIDATE_PATH,
    ExecutorCommand,
    ExecutorHeartbeatResponse,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
)
from .profile import ExecutorProfile

_RETRYABLE_STATUS_CODES = {408, 425, 429}
_REQUEST_KINDS = {
    EXECUTOR_HELLO_PATH: "hello",
    EXECUTOR_VALIDATE_PATH: "validate",
    EXECUTOR_HEARTBEAT_PATH: "heartbeat",
    EXECUTOR_POLL_PATH: "poll",
    EXECUTOR_RESULT_PATH: "result",
}
_OWNER_ACTION_CODES = {
    ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
    ProtocolErrorCode.EXECUTOR_REVOKED,
}


@dataclass(frozen=True)
class ExecutorControlError(RuntimeError):
    """One bounded control request failure without exposing the bearer."""

    message: str
    status_code: int | None = None
    protocol_error: ProtocolError | None = None
    request_kind: str | None = None
    transport_error: str | None = None

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
        self._client = client if client is not None else self._new_client()
        self._refresh_needed = False
        self._inflight: dict[httpx.AsyncClient, int] = {}
        self._retired: set[httpx.AsyncClient] = set()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._profile.control_url,
            headers={"Authorization": f"Bearer {self._profile.credential}"},
            follow_redirects=False,
            # Plain HTTP is valid only for loopback profiles; never let a
            # process-global HTTP_PROXY route that bearer off the machine.
            trust_env=self._profile.control_url.lower().startswith("https://"),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            for client in (*self._retired, self._client):
                await client.aclose()

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
                "control returned an invalid hello response",
                request_kind="hello",
            ) from exc

    async def validate(self) -> None:
        await self._post(EXECUTOR_VALIDATE_PATH, json={}, timeout=30.0)

    async def heartbeat(self) -> ExecutorHeartbeatResponse | None:
        response = await self._post(
            EXECUTOR_HEARTBEAT_PATH, json={}, timeout=30.0
        )
        if response.status_code == 204:
            return None
        try:
            return ExecutorHeartbeatResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ExecutorControlError(
                "control returned an invalid heartbeat response",
                request_kind="heartbeat",
            ) from exc

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
                "control returned an invalid command", request_kind="poll"
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
        # HTTPX snapshots system proxy settings at client construction. Recreate
        # the transport after a failure so sleep/network/proxy changes can recover.
        if self._owns_client and self._refresh_needed:
            old = self._client
            self._client = self._new_client()
            self._refresh_needed = False
            self._retired.add(old)
            if not self._inflight.get(old):
                self._retired.remove(old)
                await old.aclose()

        client = self._client
        self._inflight[client] = self._inflight.get(client, 0) + 1
        try:
            try:
                response = await client.post(path, json=json, timeout=timeout)
            except httpx.HTTPError as exc:
                if self._owns_client and client is self._client:
                    self._refresh_needed = True
                raise ExecutorControlError(
                    f"control request failed: {type(exc).__name__}",
                    request_kind=_REQUEST_KINDS.get(path),
                    transport_error=type(exc).__name__,
                ) from exc
            if response.status_code < 400:
                return response
            raise replace(
                _response_error(response), request_kind=_REQUEST_KINDS.get(path)
            )
        finally:
            count = self._inflight[client] - 1
            if count:
                self._inflight[client] = count
            else:
                self._inflight.pop(client)
                if client in self._retired:
                    self._retired.remove(client)
                    await client.aclose()


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
