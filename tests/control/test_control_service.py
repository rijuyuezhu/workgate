"""Control service lifecycle uses only the current user's managed unit/task."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from workgate.config.control import CONTROL_SETTING_NAMES
from workgate.control import service
from workgate.utils.systemd import systemd_quote


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    config = tmp_path / "control config $%.yaml"
    config.write_text(
        "mode: http\nauth_mode: oauth\noauth_admin_pin: placeholder\n"
    )
    unit = tmp_path / "systemd" / "user" / "workgate-control.service"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):
        commands.append(command)
        result = (
            f'"{unit.parent}" /etc/systemd/user\n'
            if command[-1:] == ["--value"]
            else ""
        )
        return subprocess.CompletedProcess(command, 0, result, "")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    return argparse.Namespace(config=str(config)), unit, commands


def test_control_service_cli_parses_config_on_both_sides():
    from workgate.main import _build_parser

    for args in (
        ["control", "--config", "control.yaml", "install-service"],
        ["control", "install-service", "--config", "control.yaml"],
    ):
        parsed = _build_parser().parse_args(args)
        assert parsed.handler is service.install_control_service
        assert parsed.config == "control.yaml"

    for action in ("status", "start", "stop", "restart", "uninstall"):
        parsed = _build_parser().parse_args(["control", f"{action}-service"])
        assert parsed.handler.__name__ == "<lambda>"


def test_control_systemd_unit_is_pinned_private_and_idempotent(manager):
    args, unit, commands = manager
    service.install_control_service(args)
    first = unit.read_text()
    assert first.startswith("# Managed by workgate control install-service")
    assert "workgate.main" in first and '"control"' in first
    assert systemd_quote(str(Path(sys.executable).absolute())) in first
    assert "--config" in first
    assert "$$%%.yaml" in first
    assert "UMask=0077" in first
    assert "UnsetEnvironment=" in first
    assert "WORKGATE_PORT" in first and "WORKGATE_OAUTH_ADMIN_PIN" in first
    assert "WORKGATE_CONFIG" in first
    assert all(
        f"WORKGATE_{field.upper()}" in first for field in CONTROL_SETTING_NAMES
    )
    if os.name == "posix":
        assert unit.stat().st_mode & 0o777 == 0o600
    assert commands[-3:] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "workgate-control.service"],
        ["systemctl", "--user", "restart", "workgate-control.service"],
    ]
    service.install_control_service(args)
    assert unit.read_text() == first


def test_control_service_refuses_unmanaged_unit(manager):
    args, unit, _commands = manager
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/bin/true\n")
    with pytest.raises(RuntimeError, match="unmanaged"):
        service.install_control_service(args)
    assert unit.read_text() == "[Service]\nExecStart=/bin/true\n"
    with pytest.raises(RuntimeError, match="unmanaged"):
        service.manage_control_service("uninstall")
    with pytest.raises(RuntimeError, match="unmanaged"):
        service.manage_control_service("status")


def test_control_service_requires_durable_config(manager, monkeypatch):
    args, _unit, commands = manager
    args.config = "/missing.yaml"
    with pytest.raises(RuntimeError, match="durable YAML"):
        service.install_control_service(args)
    assert commands == []
    monkeypatch.setenv("WORKGATE_PORT", "9999")
    with pytest.raises(RuntimeError, match="durable YAML"):
        service.install_control_service(args)
    assert commands == []


def test_control_service_rejects_transient_cli_overrides(manager):
    from workgate.main import _build_parser

    _args, _unit, commands = manager
    args = _build_parser().parse_args(
        ["control", "--port", "9090", "install-service"]
    )
    with pytest.raises(RuntimeError, match="CLI setting overrides"):
        service.install_control_service(args)
    assert commands == []


def test_control_service_rejects_stdio_and_mcp_modes(manager):
    args, _unit, commands = manager
    Path(args.config).write_text("mode: stdio\n")
    with pytest.raises(RuntimeError, match="mode: http"):
        service.install_control_service(args)
    assert commands == []


def test_control_service_does_not_manage_uninstalled_unit(manager, capsys):
    _args, _unit, commands = manager
    service.manage_control_service("status")
    assert "not-installed" in capsys.readouterr().out
    for action in ("start", "stop", "restart"):
        with pytest.raises(RuntimeError, match="not installed"):
            service.manage_control_service(action)
    assert all(
        command[-1] != "workgate-control.service" for command in commands
    )


def test_control_service_uninstall_preserves_config(manager):
    args, unit, commands = manager
    service.install_control_service(args)
    service.manage_control_service("status")
    service.manage_control_service("stop")
    service.manage_control_service("start")
    service.manage_control_service("restart")
    service.manage_control_service("uninstall")
    assert not unit.exists()
    assert Path(args.config).is_file()
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "workgate-control.service",
    ] in commands


def test_windows_control_service_windowless_launcher(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)
    config = tmp_path / "control.yaml"
    config.write_text("mode: http\n")
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.WindowsUserTask, "available", lambda: True)
    monkeypatch.setattr(
        service,
        "app_paths",
        lambda: argparse.Namespace(
            state_dir=tmp_path / "state", config_file=config
        ),
    )
    python = tmp_path / "Scripts" / "python.exe"
    python.parent.mkdir()
    python.touch()
    python.with_name("pythonw.exe").touch()
    monkeypatch.setattr(service.sys, "executable", str(python))
    calls = []

    class Task:
        installed = False

        def status(self):
            return {"state": "Ready"} if self.installed else None

        def install(self, command, *, cwd):
            self.installed = True
            calls.append(("install", command, cwd))

        def start(self):
            calls.append(("start",))

        def uninstall(self):
            self.installed = False
            calls.append(("uninstall",))

    task = Task()
    monkeypatch.setattr(service, "_task", lambda: task)
    service.install_control_service(argparse.Namespace(config=str(config)))
    assert calls[0][1][0].endswith("pythonw.exe")
    launcher = tmp_path / "state" / "control" / "service" / "launcher.py"
    assert launcher.is_file()
    assert "workgate.main import main" in launcher.read_text()
    assert repr(str(config)) in launcher.read_text()
    log = launcher.parent / "control.log"
    log.write_text("retain service diagnostics")
    service.manage_control_service("uninstall")
    assert not launcher.exists()
    assert log.read_text() == "retain service diagnostics"
    assert config.exists()


def test_windows_uninstall_without_owned_task_preserves_existing_files(
    tmp_path, monkeypatch
):
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.WindowsUserTask, "available", lambda: True)
    monkeypatch.setattr(
        service,
        "app_paths",
        lambda: argparse.Namespace(state_dir=tmp_path),
    )
    directory = tmp_path / "control" / "service"
    directory.mkdir(parents=True)
    launcher = directory / "launcher.py"
    log = directory / "control.log"
    launcher.write_text("do not delete an unowned file")
    log.write_text("retain log")
    called = []

    class NoTask:
        def status(self):
            return None

        def uninstall(self):
            called.append("unexpected")

    monkeypatch.setattr(service, "_task", NoTask)
    service.manage_control_service("uninstall")
    assert called == []
    assert launcher.read_text() == "do not delete an unowned file"
    assert log.read_text() == "retain log"
