from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.control_client import (
    ExecutorControlClient,
    ExecutorControlError,
)
from workgate.executor.pairing import (
    ExecutorPairingClient,
    persist_profile_before_first_hello,
    wait_for_pairing,
)
from workgate.executor.profile import ExecutorProfile, ExecutorProfileStore
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.errors import ProtocolError, ProtocolErrorCode
from workgate.protocol.executor import ExecutorHelloResponse
from workgate.protocol.ids import (
    new_device_code,
    new_executor_id,
    new_user_code,
)
from workgate.protocol.pairing import (
    PairPollSuccess,
    PairStartRequest,
    PairStartResponse,
)


def _store(tmp_path: Path) -> FileStateStore:
    return FileStateStore(lambda: tmp_path / "state")


def _config(tmp_path: Path):
    return resolve_executor_config(
        Settings(workspace_root=tmp_path / "workspace")
    )


@pytest.mark.asyncio
async def test_pairing_client_uses_public_pair_routes_and_parses_pending() -> (
    None
):
    device_code = new_device_code()
    user_code = new_user_code()
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path.endswith("/pair/start"):
            return httpx.Response(
                200,
                json={
                    "device_code": device_code,
                    "user_code": user_code,
                    "verification_uri": "https://control.test/pair",
                    "expires_in": 600,
                    "poll_interval": 2,
                },
            )
        return httpx.Response(
            202,
            json={
                "error": {
                    "code": "pairing_pending",
                    "message": "pairing approval is pending",
                }
            },
        )

    http = httpx.AsyncClient(
        base_url="https://control.test",
        transport=httpx.MockTransport(handler),
    )
    client = ExecutorPairingClient("https://control.test/", client=http)
    try:
        started = await client.start(PairStartRequest(requested_name="laptop"))
        assert started.device_code == device_code
        with pytest.raises(ExecutorControlError) as exc_info:
            await client.poll(device_code)
        assert exc_info.value.code is ProtocolErrorCode.PAIRING_PENDING
        assert device_code not in str(exc_info.value)
    finally:
        await http.aclose()

    assert calls[0][0] == "/executor/v1/pair/start"
    assert calls[0][1]["requested_name"] == "laptop"
    assert calls[1] == (
        "/executor/v1/pair/poll",
        {"device_code": device_code},
    )


@pytest.mark.asyncio
async def test_wait_for_pairing_retries_only_pending() -> None:
    device_code = new_device_code()
    started = PairStartResponse(
        device_code=device_code,
        user_code=new_user_code(),
        verification_uri="https://control.test/pair",
        expires_in=600,
        poll_interval=2,
    )
    expected = PairPollSuccess(
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    sleeps: list[float] = []

    class StubPairingClient(ExecutorPairingClient):
        def __init__(self) -> None:
            self.calls = 0

        async def poll(self, device_code: str) -> PairPollSuccess:
            assert device_code == started.device_code
            self.calls += 1
            if self.calls == 1:
                raise ExecutorControlError(
                    "pending",
                    status_code=202,
                    protocol_error=ProtocolError(
                        code=ProtocolErrorCode.PAIRING_PENDING,
                        message="pending",
                    ),
                )
            return expected

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = StubPairingClient()
    result = await wait_for_pairing(client, started, sleep=fake_sleep)

    assert result == expected
    assert client.calls == 2
    assert sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_profile_is_persisted_before_first_authenticated_hello(
    tmp_path: Path,
) -> None:
    profile_store = ExecutorProfileStore(_store(tmp_path))
    issued = PairPollSuccess(
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    hello_observations: list[ExecutorProfile | None] = []

    class RecordingClient(ExecutorControlClient):
        def __init__(self, profile: ExecutorProfile) -> None:
            self.profile = profile

        async def hello(self, message):
            assert message.workspace_root == str(
                _config(tmp_path).workspace_root
            )
            hello_observations.append(profile_store.load())
            return ExecutorHelloResponse(
                heartbeat_interval_s=15,
                offline_after_s=60,
                poll_timeout_s=25,
            )

        async def aclose(self) -> None:
            return None

    profile = await persist_profile_before_first_hello(
        control_url="https://control.test",
        pairing_result=issued,
        profile_store=profile_store,
        config=_config(tmp_path),
        client_factory=RecordingClient,
    )

    assert profile_store.load() == profile
    assert hello_observations == [profile]


@pytest.mark.asyncio
async def test_profile_write_failure_sends_no_authenticated_hello(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_store = ExecutorProfileStore(_store(tmp_path))
    issued = PairPollSuccess(
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    client_created = False

    def fail_save(_profile: ExecutorProfile) -> None:
        raise OSError("disk full")

    class ShouldNotExist(ExecutorControlClient):
        def __init__(self, profile: ExecutorProfile) -> None:
            nonlocal client_created
            client_created = True
            super().__init__(profile)

    monkeypatch.setattr(profile_store, "save", fail_save)

    with pytest.raises(OSError, match="disk full"):
        await persist_profile_before_first_hello(
            control_url="https://control.test",
            pairing_result=issued,
            profile_store=profile_store,
            config=_config(tmp_path),
            client_factory=ShouldNotExist,
        )

    assert client_created is False
    assert profile_store.load() is None
