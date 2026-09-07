"""Executor-side device pairing and profile provisioning."""

from __future__ import annotations

import asyncio
import platform
from collections.abc import Awaitable, Callable

import httpx
from pydantic import ValidationError

from .. import __version__
from ..protocol.errors import ProtocolErrorCode
from ..protocol.executor import (
    EXECUTOR_PAIR_POLL_PATH,
    EXECUTOR_PAIR_START_PATH,
)
from ..protocol.pairing import (
    PairingExecutorMetadata,
    PairPollRequest,
    PairPollSuccess,
    PairStartRequest,
    PairStartResponse,
)
from .config import ExecutorConfig
from .control_client import (
    ExecutorControlClient,
    ExecutorControlError,
    _response_error,
)
from .hello import build_executor_hello
from .profile import (
    ExecutorProfile,
    ExecutorProfileStore,
    normalize_control_url,
)


class ExecutorPairingClient:
    """Small unauthenticated HTTP client used only until a profile is issued."""

    def __init__(
        self,
        control_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.control_url = normalize_control_url(control_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.control_url,
            follow_redirects=False,
            trust_env=self.control_url.lower().startswith("https://"),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def start(self, request: PairStartRequest) -> PairStartResponse:
        response = await self._post(
            EXECUTOR_PAIR_START_PATH,
            request.model_dump(mode="json"),
        )
        try:
            return PairStartResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ExecutorControlError(
                "control returned an invalid pair-start response"
            ) from exc

    async def poll(self, device_code: str) -> PairPollSuccess:
        request = PairPollRequest(device_code=device_code)
        response = await self._post(
            EXECUTOR_PAIR_POLL_PATH,
            request.model_dump(mode="json"),
        )
        try:
            return PairPollSuccess.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ExecutorControlError(
                "control returned an invalid pair-poll response"
            ) from exc

    async def _post(self, path: str, payload: object) -> httpx.Response:
        try:
            response = await self._client.post(path, json=payload, timeout=30.0)
        except httpx.HTTPError as exc:
            raise ExecutorControlError(
                f"control request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 200:
            return response
        raise _response_error(response)


def build_pair_start_request(
    *,
    requested_name: str | None,
    existing_executor_id: str | None,
) -> PairStartRequest:
    """Build bounded diagnostic metadata without exposing local secrets."""
    return PairStartRequest(
        requested_name=requested_name,
        existing_executor_id=existing_executor_id,
        metadata=PairingExecutorMetadata(
            hostname=platform.node()[:255] or None,
            platform=platform.system()[:128] or None,
            build=__version__[:128],
        ),
    )


async def wait_for_pairing(
    client: ExecutorPairingClient,
    started: PairStartResponse,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PairPollSuccess:
    """Poll at the advertised interval until approval or a terminal failure."""
    while True:
        await sleep(float(started.poll_interval))
        try:
            return await client.poll(started.device_code)
        except ExecutorControlError as exc:
            if exc.code is ProtocolErrorCode.PAIRING_PENDING:
                continue
            raise


async def persist_profile_before_first_hello(
    *,
    control_url: str,
    pairing_result: PairPollSuccess,
    profile_store: ExecutorProfileStore,
    config: ExecutorConfig,
    client_factory: Callable[
        [ExecutorProfile], ExecutorControlClient
    ] = ExecutorControlClient,
) -> ExecutorProfile:
    """Atomically save the issued bearer before proving it with the first hello."""
    profile = ExecutorProfile(
        control_url=normalize_control_url(control_url),
        executor_id=pairing_result.executor_id,
        credential=pairing_result.credential,
    )
    profile_store.save(profile)
    client = client_factory(profile)
    try:
        await client.hello(build_executor_hello(config))
    finally:
        await client.aclose()
    return profile
