from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from workgate.config.settings import Settings
from workgate.executor import cli as executor_cli
from workgate.executor.control_client import (
    ExecutorControlClient,
    ExecutorControlError,
)
from workgate.executor.profile import ExecutorProfile, ExecutorProfileStore
from workgate.main import _build_parser
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.errors import ProtocolError, ProtocolErrorCode
from workgate.protocol.executor import ExecutorHelloResponse
from workgate.protocol.ids import (
    new_device_code,
    new_executor_id,
    new_user_code,
)
from workgate.protocol.pairing import PairPollSuccess, PairStartResponse


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
        remote_enabled=False,
    )


def _store(tmp_path: Path) -> FileStateStore:
    return FileStateStore(lambda: tmp_path / "state")


def _args(control_url: str = "https://control.test") -> argparse.Namespace:
    return argparse.Namespace(control_url=control_url, name="Laptop")


def _profile(control_url: str = "https://control.test") -> ExecutorProfile:
    return ExecutorProfile(
        control_url=control_url,
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )


def _hello_response() -> ExecutorHelloResponse:
    return ExecutorHelloResponse(
        heartbeat_interval_s=15,
        offline_after_s=60,
        poll_timeout_s=25,
    )


def test_root_parser_exposes_final_executor_connect_and_run() -> None:
    connect = _build_parser().parse_args(
        ["executor", "connect", "https://control.test", "--name", "Laptop"]
    )
    assert connect.command == "executor"
    assert connect.executor_command == "connect"
    assert connect.control_url == "https://control.test"
    assert connect.name == "Laptop"

    run = _build_parser().parse_args(["executor", "run"])
    assert run.command == "executor"
    assert run.executor_command == "run"


@pytest.mark.asyncio
async def test_connect_keeps_valid_existing_profile_without_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    existing = _profile()
    ExecutorProfileStore(store).save(existing)
    hello_calls = 0

    class ExistingClient(ExecutorControlClient):
        def __init__(self, profile: ExecutorProfile) -> None:
            assert profile == existing

        async def hello(self, message):
            del message
            nonlocal hello_calls
            hello_calls += 1
            return _hello_response()

        async def aclose(self) -> None:
            return None

    class PairingMustNotStart:
        def __init__(self, _control_url: str) -> None:
            raise AssertionError(
                "valid existing profile unexpectedly re-paired"
            )

    monkeypatch.setattr(
        executor_cli, "settings_from_args", lambda *_a, **_k: settings
    )
    monkeypatch.setattr(executor_cli, "get_state_store", lambda: store)
    monkeypatch.setattr(executor_cli, "ExecutorControlClient", ExistingClient)
    monkeypatch.setattr(
        executor_cli, "ExecutorPairingClient", PairingMustNotStart
    )

    await executor_cli._connect(_args())

    assert hello_calls == 1
    output = capsys.readouterr().out
    assert existing.executor_id in output
    assert existing.credential not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        (None, None),
        (ProtocolErrorCode.UNSUPPORTED_PROTOCOL, 400),
    ],
)
async def test_connect_does_not_repair_transient_or_protocol_incompatible_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: ProtocolErrorCode | None,
    status_code: int | None,
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    existing = _profile()
    ExecutorProfileStore(store).save(existing)

    class RejectedClient(ExecutorControlClient):
        def __init__(self, _profile: ExecutorProfile) -> None:
            return None

        async def hello(self, message):
            del message
            error = (
                None
                if code is None
                else ProtocolError(code=code, message="owner action")
            )
            raise ExecutorControlError(
                "control unavailable or incompatible",
                status_code=status_code,
                protocol_error=error,
            )

        async def aclose(self) -> None:
            return None

    class PairingMustNotStart:
        def __init__(self, _control_url: str) -> None:
            raise AssertionError(
                "non-pairing failure unexpectedly started pairing"
            )

    monkeypatch.setattr(
        executor_cli, "settings_from_args", lambda *_a, **_k: settings
    )
    monkeypatch.setattr(executor_cli, "get_state_store", lambda: store)
    monkeypatch.setattr(executor_cli, "ExecutorControlClient", RejectedClient)
    monkeypatch.setattr(
        executor_cli, "ExecutorPairingClient", PairingMustNotStart
    )

    with pytest.raises(ExecutorControlError):
        await executor_cli._connect(_args())


@pytest.mark.asyncio
async def test_revoked_profile_starts_pairing_with_existing_id_hint_without_secret_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    existing = _profile()
    ExecutorProfileStore(store).save(existing)
    device_code = new_device_code()
    user_code = new_user_code()
    replacement_credential = new_executor_credential()
    observed_existing_id: str | None = None
    pairing_closed = False

    class RevokedClient(ExecutorControlClient):
        def __init__(self, _profile: ExecutorProfile) -> None:
            return None

        async def hello(self, message):
            del message
            raise ExecutorControlError(
                "revoked",
                status_code=403,
                protocol_error=ProtocolError(
                    code=ProtocolErrorCode.EXECUTOR_REVOKED,
                    message="revoked",
                ),
            )

        async def aclose(self) -> None:
            return None

    class PairingClient:
        def __init__(self, control_url: str) -> None:
            assert control_url == "https://control.test"

        async def start(self, request):
            nonlocal observed_existing_id
            observed_existing_id = request.existing_executor_id
            assert request.requested_name == "Laptop"
            return PairStartResponse(
                device_code=device_code,
                user_code=user_code,
                verification_uri="https://control.test/pair",
                expires_in=600,
                poll_interval=2,
            )

        async def aclose(self) -> None:
            nonlocal pairing_closed
            pairing_closed = True

    issued = PairPollSuccess(
        executor_id=existing.executor_id,
        credential=replacement_credential,
    )

    async def fake_wait(_client, started: PairStartResponse) -> PairPollSuccess:
        assert started.device_code == device_code
        return issued

    async def fake_persist(**kwargs) -> ExecutorProfile:
        assert kwargs["pairing_result"] == issued
        profile = ExecutorProfile(
            control_url=kwargs["control_url"],
            executor_id=issued.executor_id,
            credential=issued.credential,
        )
        kwargs["profile_store"].save(profile)
        return profile

    monkeypatch.setattr(
        executor_cli, "settings_from_args", lambda *_a, **_k: settings
    )
    monkeypatch.setattr(executor_cli, "get_state_store", lambda: store)
    monkeypatch.setattr(executor_cli, "ExecutorControlClient", RevokedClient)
    monkeypatch.setattr(executor_cli, "ExecutorPairingClient", PairingClient)
    monkeypatch.setattr(executor_cli, "wait_for_pairing", fake_wait)
    monkeypatch.setattr(
        executor_cli,
        "persist_profile_before_first_hello",
        fake_persist,
    )

    await executor_cli._connect(_args())

    assert observed_existing_id == existing.executor_id
    assert pairing_closed is True
    saved = ExecutorProfileStore(store).load()
    assert saved is not None
    assert saved.credential == replacement_credential
    output = capsys.readouterr().out
    assert "https://control.test/pair" in output
    assert user_code in output
    assert device_code not in output
    assert replacement_credential not in output
