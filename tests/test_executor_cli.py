from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from workgate.config.settings import Settings
from workgate.executor import cli as executor_cli
from workgate.executor.control_client import (
    ExecutorControlClient,
    ExecutorControlError,
)
from workgate.executor.profile import ExecutorProfile, ExecutorProfileStore
from workgate.executor.service import ExecutorServiceState
from workgate.main import _build_parser
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.errors import ProtocolError, ProtocolErrorCode
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


def test_run_async_reports_command_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail() -> None:
        raise RuntimeError("control unavailable")

    with pytest.raises(SystemExit) as caught:
        executor_cli._run_async(fail())

    assert caught.value.code == 1
    assert capsys.readouterr().err == (
        "Status: executor command failed: control unavailable\n"
    )


@pytest.mark.asyncio
async def test_run_requires_paired_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)

    class Runtime:
        connection = None

        @asynccontextmanager
        async def lifespan(self):
            yield

    monkeypatch.setattr(
        executor_cli, "settings_from_args", lambda *_a, **_k: settings
    )
    monkeypatch.setattr(
        executor_cli, "build_executor_runtime", lambda _config: Runtime()
    )

    with pytest.raises(RuntimeError, match="executor is not paired"):
        await executor_cli._run(argparse.Namespace())


@pytest.mark.asyncio
async def test_managed_run_clears_workgate_environment_before_loading_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    observed: dict[str, str] = {}

    class Runtime:
        connection = None

        @asynccontextmanager
        async def lifespan(self):
            yield

    monkeypatch.setenv("WORKGATE_STATE_DIR", "/wrong/from-environment")
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "secret")

    def fake_settings_from_args(*_args, **_kwargs):
        observed.update(
            {
                name: value
                for name, value in executor_cli.os.environ.items()
                if name.startswith("WORKGATE_")
            }
        )
        return settings

    monkeypatch.setattr(
        executor_cli, "settings_from_args", fake_settings_from_args
    )
    monkeypatch.setattr(
        executor_cli, "build_executor_runtime", lambda _config: Runtime()
    )

    with pytest.raises(RuntimeError, match="executor is not paired"):
        await executor_cli._run(argparse.Namespace(managed_service=True))

    assert observed == {}


@pytest.mark.asyncio
async def test_run_raises_owner_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    owner_action = RuntimeError("executor replaced")

    class Connection:
        async def wait_owner_action(self) -> BaseException:
            return owner_action

    class Runtime:
        connection = Connection()

        @asynccontextmanager
        async def lifespan(self):
            yield

    monkeypatch.setattr(
        executor_cli, "settings_from_args", lambda *_a, **_k: settings
    )
    monkeypatch.setattr(
        executor_cli, "build_executor_runtime", lambda _config: Runtime()
    )

    with pytest.raises(RuntimeError, match="executor replaced") as caught:
        await executor_cli._run(argparse.Namespace(managed_service=False))

    assert caught.value is owner_action


def test_run_async_maps_keyboard_interrupt_to_130() -> None:
    async def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(SystemExit) as caught:
        executor_cli._run_async(interrupted())

    assert caught.value.code == 130


def test_connect_from_args_runs_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args()
    observed: list[argparse.Namespace] = []

    async def fake_connect(value: argparse.Namespace) -> None:
        observed.append(value)

    monkeypatch.setattr(executor_cli, "_connect", fake_connect)

    executor_cli._connect_from_args(args)

    assert observed == [args]


@pytest.mark.parametrize(
    "action",
    ["install", "uninstall", "status", "start", "stop", "restart", "logs"],
)
def test_service_actions_dispatch_and_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    settings = _settings(tmp_path)
    status = executor_cli.ExecutorServiceStatus(
        backend="test-backend",
        state=ExecutorServiceState.RUNNING,
        installed=True,
        running=True,
        detail="healthy",
        service_file="/tmp/workgate.service",
        log_path="/tmp/workgate.log",
        runtime_current=False,
    )

    class Manager:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def install(self):
            self.calls.append("install")
            return argparse.Namespace(status=status)

        def uninstall(self):
            self.calls.append("uninstall")
            return status

        def status(self):
            self.calls.append("status")
            return status

        def start(self):
            self.calls.append("start")
            return status

        def stop(self):
            self.calls.append("stop")
            return status

        def restart(self):
            self.calls.append("restart")
            return status

        def logs(self, *, lines: int):
            self.calls.append(f"logs:{lines}")
            return "recent executor log"

    manager = Manager()

    def fake_manager(active_settings: Settings) -> Manager:
        assert active_settings is settings
        return manager

    monkeypatch.setattr(
        executor_cli, "settings_from_args", lambda *_a, **_k: settings
    )
    monkeypatch.setattr(executor_cli, "ExecutorServiceManager", fake_manager)

    executor_cli._run_service_action(argparse.Namespace(lines=25), action)

    expected_call = "logs:25" if action == "logs" else action
    assert manager.calls == [expected_call]
    output = capsys.readouterr().out
    if action == "logs":
        assert output == "recent executor log\n"
    else:
        assert "State: running" in output
        assert "Backend: test-backend" in output
        assert "Service: /tmp/workgate.service" in output
        assert "Log: /tmp/workgate.log" in output
        assert "Runtime: stale" in output
        assert "Detail: healthy" in output


def test_service_status_render_omits_optional_fields_for_current_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = executor_cli.ExecutorServiceStatus(
        backend="test-backend",
        state=ExecutorServiceState.STOPPED,
        installed=True,
        running=False,
    )

    executor_cli._print_service_status(object(), status)  # type: ignore[arg-type]

    assert capsys.readouterr().out == (
        "State: stopped\nBackend: test-backend\nRuntime: current\n"
    )


def test_service_action_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Manager:
        def status(self):
            raise RuntimeError("service unavailable")

    monkeypatch.setattr(
        executor_cli, "_service_manager", lambda _args: Manager()
    )

    with pytest.raises(SystemExit) as caught:
        executor_cli._run_service_action(argparse.Namespace(), "status")

    assert caught.value.code == 1
    assert capsys.readouterr().err == (
        "Status: executor command failed: service unavailable\n"
    )


def test_service_action_maps_keyboard_interrupt_to_130(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Manager:
        def status(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        executor_cli, "_service_manager", lambda _args: Manager()
    )

    with pytest.raises(SystemExit) as caught:
        executor_cli._run_service_action(argparse.Namespace(), "status")

    assert caught.value.code == 130


def test_service_action_rejects_unknown_action(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        executor_cli, "_service_manager", lambda _args: object()
    )

    with pytest.raises(SystemExit) as caught:
        executor_cli._run_service_action(argparse.Namespace(), "unknown")

    assert caught.value.code == 1
    assert "unknown" in capsys.readouterr().err


def test_root_parser_exposes_executor_runtime_and_service_lifecycle() -> None:
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

    for command in (
        "install-service",
        "status",
        "start",
        "stop",
        "restart",
        "uninstall-service",
    ):
        parsed = _build_parser().parse_args(["executor", command])
        assert parsed.executor_command == command

    logs = _build_parser().parse_args(["executor", "logs", "--lines", "25"])
    assert logs.executor_command == "logs"
    assert logs.lines == 25


@pytest.mark.asyncio
async def test_connect_rejects_existing_profile_for_different_control_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = _store(tmp_path)
    existing = _profile("https://first-control.test")
    ExecutorProfileStore(store).save(existing)

    class PairingMustNotStart:
        def __init__(self, _control_url: str) -> None:
            raise AssertionError(
                "mismatched profile unexpectedly started pairing"
            )

    monkeypatch.setattr(
        executor_cli, "settings_from_args", lambda *_a, **_k: settings
    )
    monkeypatch.setattr(executor_cli, "get_state_store", lambda: store)
    monkeypatch.setattr(
        executor_cli, "ExecutorPairingClient", PairingMustNotStart
    )

    with pytest.raises(RuntimeError, match="different control URL"):
        await executor_cli._connect(_args("https://second-control.test"))


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
    validate_calls = 0

    class ExistingClient(ExecutorControlClient):
        def __init__(self, profile: ExecutorProfile) -> None:
            assert profile == existing

        async def validate(self) -> None:
            nonlocal validate_calls
            validate_calls += 1

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

    assert validate_calls == 1
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

        async def validate(self) -> None:
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

        async def validate(self) -> None:
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
        "persist_profile_and_validate",
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
