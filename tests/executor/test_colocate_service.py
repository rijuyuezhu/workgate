"""The colocate systemd installer owns only one user unit."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from workgate.colocate import service
from workgate.executor.profile import ExecutorProfile, ExecutorProfileStore
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.ids import new_executor_id


@pytest.fixture
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_root = tmp_path / "config"
    state_root = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.delenv("WORKGATE_CONFIG", raising=False)
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(
        service.shutil, "which", lambda name: "/usr/bin/systemctl"
    )

    executor_config = tmp_path / "executor.yaml"
    executor_state = tmp_path / "executor-state"
    executor_config.write_text(f"state_dir: {executor_state}\n")
    control_config = tmp_path / "control.yaml"
    control_config.write_text("mode: mcp\n")

    commands: list[list[str]] = []
    unit_dir = tmp_path / "manager-config" / "systemd" / "user"
    unit_path = unit_dir / "workgate-colocate.service"

    def fake_run(command, **_kwargs):
        commands.append(command)
        stdout = ""
        if command == [
            "systemctl",
            "--user",
            "show",
            "--property=UnitPath",
            "--value",
        ]:
            stdout = f'"{unit_dir}" /etc/systemd/user\n'
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(service.subprocess, "run", fake_run)

    def pair() -> None:
        ExecutorProfileStore(FileStateStore(lambda: executor_state)).save(
            ExecutorProfile(
                control_url="http://127.0.0.1:8765",
                executor_id=new_executor_id(),
                credential=new_executor_credential(),
            )
        )

    args = argparse.Namespace(
        control_config=str(control_config),
        executor_config=str(executor_config),
    )
    return args, commands, pair, unit_path


def test_install_service_requires_pairing(installer):
    args, commands, _pair, unit_path = installer
    with pytest.raises(RuntimeError, match="pair the executor"):
        service.install_colocate_service(args)
    assert commands == [["systemctl", "--user", "show-environment"]]
    assert not unit_path.exists()


def test_install_service_creates_unit_and_is_idempotent(installer, monkeypatch):
    args, commands, pair, unit_path = installer
    pair()
    service.install_colocate_service(args)
    unit = unit_path
    content = unit.read_text()
    assert content.startswith("# Managed by workgate colocate install-service")
    assert "Description=Workgate colocated control and executor" in content
    assert "Restart=always" in content
    assert 'Environment="PYTHONUNBUFFERED=1"' in content
    assert "WantedBy=default.target" in content
    assert "control.yaml" in content
    assert "executor.yaml" in content
    assert 'ExecStart="' + str(Path(sys.executable).absolute()) + '"' in content
    assert "workgate.main" in content
    assert unit.stat().st_mode & 0o777 == 0o600
    assert commands == [
        ["systemctl", "--user", "show-environment"],
        ["systemctl", "--user", "show", "--property=UnitPath", "--value"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "workgate-colocate.service"],
        ["systemctl", "--user", "restart", "workgate-colocate.service"],
    ]

    service.install_colocate_service(args)
    assert len(commands) == 10
    assert unit.read_text() == content


def test_install_service_does_not_overwrite_unmanaged_unit(installer):
    args, commands, pair, unit = installer
    pair()
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/bin/true\n")
    with pytest.raises(RuntimeError, match="refusing to overwrite unmanaged"):
        service.install_colocate_service(args)
    assert unit.read_text() == "[Service]\nExecStart=/bin/true\n"
    assert len(commands) == 2


def test_install_service_quotes_literal_paths(installer):
    args, _commands, pair, unit = installer
    pair()
    args.control_config = str(
        Path(args.control_config).with_name("my $% config.yaml")
    )
    Path(args.control_config).write_text("mode: mcp\n")
    service.install_colocate_service(args)
    assert "my $$%% config.yaml" in unit.read_text()


def test_install_service_handles_frozen_executable(installer, monkeypatch):
    args, _commands, pair, unit = installer
    pair()
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    service.install_colocate_service(args)
    content = unit.read_text()
    assert ' "colocate" ' in content
    assert "workgate.main" not in content


def test_install_service_refuses_transient_env_config(installer, monkeypatch):
    args, commands, pair, _unit = installer
    pair()
    monkeypatch.setenv("WORKGATE_PORT", "1234")
    with pytest.raises(RuntimeError, match="durable YAML"):
        service.install_colocate_service(args)
    assert commands == []


def test_install_service_checks_systemd_availability(installer, monkeypatch):
    args, commands, pair, _unit = installer
    pair()
    monkeypatch.setattr(service.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="requires Linux systemd"):
        service.install_colocate_service(args)
    assert commands == []


def test_install_service_reports_systemctl_error(installer, monkeypatch):
    args, commands, pair, unit = installer
    pair()

    def fake_failure(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 1, "", "manager unavailable"
        )

    monkeypatch.setattr(service.subprocess, "run", fake_failure)
    with pytest.raises(RuntimeError, match="manager unavailable"):
        service.install_colocate_service(args)
    assert not unit.exists()


def test_install_service_requires_real_config_file(installer):
    args, commands, pair, _unit = installer
    pair()
    args.executor_config = "/missing/executor.yaml"
    with pytest.raises(FileNotFoundError):
        service.install_colocate_service(args)
    assert commands == [["systemctl", "--user", "show-environment"]]


def test_install_service_pins_existing_default_yaml(installer):
    args, _commands, pair, unit = installer
    pair()
    default_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "workgate"
    (default_dir / "executor").mkdir(parents=True)
    control_default = default_dir / "config.yaml"
    executor_default = default_dir / "executor" / "config.yaml"
    control_default.write_text(Path(args.control_config).read_text())
    executor_default.write_text(Path(args.executor_config).read_text())
    args.control_config = None
    args.executor_config = None

    service.install_colocate_service(args)
    content = unit.read_text()
    assert str(control_default) in content
    assert str(executor_default) in content


def test_install_service_rejects_invalid_systemd_unit_path(
    installer, monkeypatch
):
    args, _commands, pair, unit = installer
    pair()
    original_run = service.subprocess.run

    def fake_run(command, **kwargs):
        if command[-3:] == ["show", "--property=UnitPath", "--value"]:
            return subprocess.CompletedProcess(
                command, 0, "/tmp/random-dir\n", ""
            )
        return original_run(command, **kwargs)

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="persistent user unit path"):
        service.install_colocate_service(args)
    assert not unit.exists()


def test_manage_colocate_user_unit_lifecycle(installer):
    args, commands, pair, unit = installer
    pair()
    service.install_colocate_service(args)
    service.manage_colocate_service("status")
    service.manage_colocate_service("stop")
    service.manage_colocate_service("start")
    service.manage_colocate_service("restart")
    assert unit.is_file()
    service.manage_colocate_service("uninstall")
    assert not unit.exists()
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "workgate-colocate.service",
    ] in commands


def test_status_distinguishes_absent_from_inactive_user_unit(installer, capsys):
    _args, commands, _pair, unit = installer
    service.manage_colocate_service("status")
    assert capsys.readouterr().out.strip() == "Colocate service: not-installed"
    assert [
        "systemctl",
        "--user",
        "is-active",
        "workgate-colocate.service",
    ] not in commands
    unit.parent.mkdir(parents=True)
    unit.write_text("# Managed by workgate colocate install-service\n")
    service.manage_colocate_service("status")
    assert capsys.readouterr().out.strip() == "Colocate service: unknown"
    assert [
        "systemctl",
        "--user",
        "is-active",
        "workgate-colocate.service",
    ] in commands


@pytest.mark.parametrize(
    "manager_output", ["", "  \n", "unknown\n", "not-installed\n"]
)
def test_existing_colocate_unit_with_unreadable_manager_status_is_not_absent(
    installer, capsys, monkeypatch, manager_output
):
    _args, _commands, _pair, unit = installer
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("# Managed by workgate colocate install-service\n")
    observed = []
    old_run = service.subprocess.run

    def run(command, **kwargs):
        if command[-2:] == ["is-active", "workgate-colocate.service"]:
            observed.append(command)
            return subprocess.CompletedProcess(command, 3, manager_output, "")
        return old_run(command, **kwargs)

    monkeypatch.setattr(service.subprocess, "run", run)
    service.manage_colocate_service("status")
    assert capsys.readouterr().out.strip() == "Colocate service: unknown"
    assert [
        "systemctl",
        "--user",
        "is-active",
        "workgate-colocate.service",
    ] in observed


@pytest.mark.skipif(os.name == "nt", reason="POSIX systemd unit symlink")
def test_dangling_colocate_unit_symlink_is_not_treated_as_absent(
    installer, capsys, monkeypatch
):
    _args, _commands, _pair, unit = installer
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.symlink_to(unit.parent / "missing-unit-target")
    service.manage_colocate_service("status")
    assert capsys.readouterr().out.strip() != "Colocate service: not-installed"
