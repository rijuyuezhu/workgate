"""Colocate first run must own the temporary Control and never orphan a service."""

import argparse
import os
import subprocess
from pathlib import Path
from typing import cast

import pytest

from workgate.colocate import first_run
from workgate.main import _build_parser


@pytest.fixture(autouse=True)
def clean_workgate_env(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("WORKGATE_"):
            monkeypatch.delenv(name)


def args(tmp_path):
    return argparse.Namespace(
        control_config=str(tmp_path / "control.yaml"),
        executor_config=str(tmp_path / "executor.yaml"),
        name="Colocated workstation",
    )


def _mock_setup(tmp_path, monkeypatch, *, ready=True, paired=True):
    parsed = args(tmp_path)
    events = []

    def init(_path):
        events.append("initialize")

    class Settings:
        host = "127.0.0.1"
        port = 8765
        ui_enabled = True
        resolved_base_url = "http://127.0.0.1:8765"
        ui_path = "/ui"

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append("wait")
            return 0

    def launch(command, **_kwargs):
        events.append(("launch", command))
        return FakeProcess()

    def pair(_args, url):
        events.append(("connect", url))
        if not paired:
            raise SystemExit(1)

    monkeypatch.setattr(
        first_run,
        "_reject_existing_services",
        lambda _args: events.append("check-services"),
    )
    monkeypatch.setattr(first_run, "_initialize_control", init)
    monkeypatch.setattr(first_run, "load_settings", lambda *a, **k: Settings())
    monkeypatch.setattr(
        first_run.socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError()),
    )
    monkeypatch.setattr(first_run.subprocess, "Popen", launch)
    monkeypatch.setattr(first_run, "_connect", pair)

    def wait(url, _proc):
        events.append(("ready", url))
        if not ready:
            raise TimeoutError("not ready")

    monkeypatch.setattr(first_run, "_wait_ready", wait)
    from workgate.colocate import service

    monkeypatch.setattr(
        service,
        "install_colocate_service",
        lambda _args: events.append("install"),
    )
    return parsed, events


def test_colocate_setup_cli_accepts_config_before_or_after_action():
    for argv in (
        ["colocate", "--control-config", "control.yaml", "setup"],
        ["colocate", "setup", "--control-config", "control.yaml"],
    ):
        parsed = _build_parser().parse_args(argv)
        assert parsed.handler is first_run.setup_colocate
        assert parsed.control_config == "control.yaml"


def test_colocate_pairing_then_stops_temp_control_before_service(
    tmp_path, monkeypatch
):
    parsed, events = _mock_setup(tmp_path, monkeypatch)
    first_run.setup_colocate(parsed)
    assert events[:2] == ["check-services", "initialize"]
    assert events[2][0] == "launch"
    assert "-m" in events[2][1]
    assert "workgate.colocate.temporary_control" in events[2][1]
    assert events[3] == ("ready", "http://127.0.0.1:8765")
    assert events[4] == ("connect", "http://127.0.0.1:8765")
    assert events[5:] == ["terminate", "wait", "install"]


@pytest.mark.parametrize("ready,paired", [(False, True), (True, False)])
def test_colocate_failure_never_installs_service(
    tmp_path, monkeypatch, ready, paired
):
    parsed, events = _mock_setup(
        tmp_path, monkeypatch, ready=ready, paired=paired
    )
    with pytest.raises((SystemExit, TimeoutError)):
        first_run.setup_colocate(parsed)
    assert "terminate" in events and "wait" in events
    assert "install" not in events


def test_colocate_rejects_existing_listener(tmp_path, monkeypatch):
    parsed, events = _mock_setup(tmp_path, monkeypatch)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(
        first_run.socket, "create_connection", lambda *a, **k: Connection()
    )
    with pytest.raises(RuntimeError, match="occupied"):
        first_run.setup_colocate(parsed)
    assert not any(
        isinstance(event, tuple) and event[0] == "launch" for event in events
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.2.1"])
def test_colocate_requires_loopback_listener(host):
    with pytest.raises(RuntimeError, match="loopback-only"):
        first_run._local_host(host)


def test_temporary_control_is_unsupervised(monkeypatch):
    from workgate.colocate import temporary_control

    monkeypatch.setattr(
        temporary_control.sys, "argv", ["temporary_control", "config.yaml"]
    )

    class Parsed:
        def __init__(self):
            self.handler = lambda args: called.append(args)

    called = []
    monkeypatch.setattr(
        temporary_control,
        "_build_parser",
        lambda: argparse.Namespace(parse_args=lambda argv: Parsed()),
    )
    temporary_control.main()
    assert len(called) == 1
    assert not hasattr(called[0], "_workgate_argv")


def test_colocate_service_conflict_stops_before_config_creation(
    tmp_path, monkeypatch
):
    parsed, events = _mock_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        first_run,
        "_reject_existing_services",
        lambda _args: (_ for _ in ()).throw(RuntimeError("already installed")),
    )
    with pytest.raises(RuntimeError, match="already installed"):
        first_run.setup_colocate(parsed)
    assert events == []


@pytest.mark.skipif(os.name == "nt", reason="Linux systemd unit paths")
def test_linux_control_unit_conflict_even_when_not_running(
    tmp_path, monkeypatch
):
    from workgate.colocate import service as colocate_service

    monkeypatch.setattr(first_run.sys, "platform", "linux")
    monkeypatch.setattr(first_run, "_control_service_installed", lambda: True)
    monkeypatch.setattr(
        colocate_service,
        "_systemctl",
        lambda *a: subprocess.CompletedProcess(
            [], 0, f"{tmp_path / 'systemd' / 'user'}\n", ""
        ),
    )
    with pytest.raises(RuntimeError, match="already installed"):
        first_run._reject_existing_services(args(tmp_path))


def test_local_url_persisted_to_same_executor_config(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(
        first_run, "_run_cli", lambda *args, **k: commands.append(args)
    )
    first_run._connect(args(tmp_path), "http://127.0.0.1:8765")
    assert commands == [
        (
            "executor",
            "connect",
            "http://127.0.0.1:8765",
            "--config",
            str(tmp_path / "executor.yaml"),
            "--name",
            "Colocated workstation",
        ),
    ]


def test_colocate_control_config_flags_preserved_before_and_after_action():
    for argv in (
        [
            "colocate",
            "--control-config",
            "control.yaml",
            "--executor-config",
            "executor.yaml",
            "setup",
        ],
        [
            "colocate",
            "setup",
            "--control-config",
            "control.yaml",
            "--executor-config",
            "executor.yaml",
        ],
    ):
        parsed = _build_parser().parse_args(argv)
        assert parsed.control_config == "control.yaml"
        assert parsed.executor_config == "executor.yaml"


def test_windows_native_control_service_conflict_is_fail_closed(
    tmp_path, monkeypatch
):
    from workgate.colocate import service as colocate_service

    monkeypatch.setattr(first_run.sys, "platform", "win32")
    monkeypatch.setattr(first_run, "_control_service_installed", lambda: True)
    monkeypatch.setattr(
        colocate_service,
        "_windows_task",
        lambda: argparse.Namespace(status=lambda: None),
    )
    with pytest.raises(RuntimeError, match="already installed"):
        first_run._reject_existing_services(args(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="Linux systemd unit paths")
def test_linux_stopped_executor_service_conflict(tmp_path, monkeypatch):
    from workgate.colocate import service as colocate_service
    from workgate.executor import service as executor_service

    monkeypatch.setattr(first_run.sys, "platform", "linux")
    monkeypatch.setattr(first_run, "_control_service_installed", lambda: False)
    monkeypatch.setattr(
        colocate_service,
        "_systemctl",
        lambda *a: subprocess.CompletedProcess(
            [], 0, f"{tmp_path / 'systemd' / 'user'}\n", ""
        ),
    )
    config = tmp_path / "executor.yaml"
    config.write_text(f"state_dir: {tmp_path / 'executor-state'}\n")
    monkeypatch.setattr(
        executor_service,
        "ExecutorServiceManager",
        lambda cfg: argparse.Namespace(
            status=lambda: argparse.Namespace(installed=True)
        ),
    )
    with pytest.raises(RuntimeError, match="separate Executor service"):
        first_run._reject_existing_services(args(tmp_path))


def test_cleanup_is_safe_when_process_already_gone():
    class Ended:
        def poll(self):
            return 0

        def terminate(self):
            raise AssertionError("must not terminate a dead process")

    first_run._stop(cast("subprocess.Popen[bytes]", Ended()))


def test_cleanup_handles_process_exit_race():
    class Exiting:
        def poll(self):
            return None

        def terminate(self):
            raise ProcessLookupError("already gone")

        def wait(self, timeout=None):
            return 0

    first_run._stop(cast("subprocess.Popen[bytes]", Exiting()))


def test_frozen_colocate_setup_rejected_before_managing_services(
    tmp_path, monkeypatch
):
    parsed, events = _mock_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(first_run.sys, "frozen", True, raising=False)
    with pytest.raises(RuntimeError, match="source installation"):
        first_run.setup_colocate(parsed)
    assert events == []


def test_cleanup_kills_stalled_temporary_control():
    events = []

    class Stuck:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if timeout == 12:
                raise subprocess.TimeoutExpired(["temporary_control"], timeout)

        def kill(self):
            events.append("kill")

    first_run._stop(cast("subprocess.Popen[bytes]", Stuck()))
    assert events == ["terminate", ("wait", 12), "kill", ("wait", 5)]


def test_windows_existing_colocate_task_conflict(tmp_path, monkeypatch):
    from workgate.colocate import service as colocate_service

    monkeypatch.setattr(first_run.sys, "platform", "win32")
    monkeypatch.setattr(
        first_run,
        "_control_service_installed",
        lambda: (_ for _ in ()).throw(
            AssertionError(
                "no Control subprocess needed when Colocate is installed"
            )
        ),
    )
    monkeypatch.setattr(
        colocate_service,
        "_windows_task",
        lambda: argparse.Namespace(status=lambda: {"state": "Stopped"}),
    )
    with pytest.raises(RuntimeError, match="already installed"):
        first_run._reject_existing_services(args(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="Linux systemd unit paths")
def test_linux_clean_native_service_preflight_allows_setup(
    tmp_path, monkeypatch
):
    from workgate.colocate import service as colocate_service
    from workgate.executor import service as executor_service

    monkeypatch.setattr(first_run.sys, "platform", "linux")
    monkeypatch.setattr(first_run, "_control_service_installed", lambda: False)
    monkeypatch.setattr(
        colocate_service,
        "_systemctl",
        lambda *a: subprocess.CompletedProcess(
            [], 0, f"{tmp_path / 'systemd' / 'user'}\n", ""
        ),
    )
    monkeypatch.setattr(
        executor_service,
        "ExecutorServiceManager",
        lambda cfg: argparse.Namespace(
            status=lambda: argparse.Namespace(installed=False)
        ),
    )
    parsed = args(tmp_path)
    Path(parsed.executor_config).write_text(
        f"state_dir: {tmp_path / 'executor-state'}\n"
    )
    first_run._reject_existing_services(parsed)


def test_control_status_uses_public_cli_and_fails_closed(monkeypatch):
    responses = [
        "Control service: not-installed\n",
        "Control service: inactive\n",
        "unexpected status",
    ]
    observed = []

    def run(*argv, quiet=False):
        observed.append((argv, quiet))
        return responses.pop(0)

    monkeypatch.setattr(first_run, "_run_cli", run)
    assert first_run._control_service_installed() is False
    assert first_run._control_service_installed() is True
    with pytest.raises(RuntimeError, match="unexpected Control service status"):
        first_run._control_service_installed()
    assert observed == [
        (("control", "status-service"), True),
        (("control", "status-service"), True),
        (("control", "status-service"), True),
    ]


def test_public_cli_propagates_failure_without_service_install(monkeypatch):
    def reject(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(first_run.subprocess, "run", reject)
    with pytest.raises(subprocess.CalledProcessError):
        first_run._initialize_control(Path("control.yaml"))
    with pytest.raises(subprocess.CalledProcessError):
        first_run._connect(
            argparse.Namespace(name="node", executor_config=None),
            "http://127.0.0.1:8765",
        )


def test_control_initialization_runs_public_subprocess(monkeypatch, tmp_path):
    commands = []

    def fake(*args, **kwargs):
        commands.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "")

    monkeypatch.setattr(first_run.subprocess, "run", fake)
    path = tmp_path / "private" / "control.yaml"
    first_run._initialize_control(path)
    assert commands[0][0][0] == [
        first_run.sys.executable,
        "-m",
        "workgate.main",
        "control",
        "init-config",
        "--config",
        str(path),
    ]
    assert commands[0][1]["check"] is True
    assert commands[0][1]["capture_output"] is True


def test_failed_quiet_control_cli_preserves_actionable_stderr(monkeypatch):
    def reject(*a, **kw):
        raise subprocess.CalledProcessError(
            1, a, stderr="private config must be 0600"
        )

    monkeypatch.setattr(first_run.subprocess, "run", reject)
    with pytest.raises(RuntimeError, match="private config must be 0600"):
        first_run._initialize_control(Path("config.yaml"))


@pytest.mark.parametrize(
    "arguments",
    [["temporary_control"], ["temporary_control", "one.yaml", "extra.yaml"]],
)
def test_temporary_control_rejects_missing_or_extra_config_arguments(
    monkeypatch, arguments
):
    from workgate.colocate import temporary_control

    monkeypatch.setattr(temporary_control.sys, "argv", arguments)
    with pytest.raises(SystemExit, match="requires one config path"):
        temporary_control.main()
