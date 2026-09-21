from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest

from workgate.config.settings import Settings
from workgate.executor import service
from workgate.executor.profile import ExecutorProfile, ExecutorProfileStore
from workgate.executor.service import (
    ExecutorServiceManager,
    ExecutorServiceState,
)
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.ids import new_executor_id


def _settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
        **kwargs,
    )


def _profile() -> ExecutorProfile:
    return ExecutorProfile(
        control_url="https://control.test",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )


def _paired_manager(
    tmp_path: Path,
    *,
    system: str,
    executable: Path | None = None,
    **settings_kwargs,
) -> tuple[ExecutorServiceManager, ExecutorProfile]:
    manager = ExecutorServiceManager(
        _settings(tmp_path, **settings_kwargs),
        home=tmp_path / "home",
        environ={},
        system=system,
        executable=executable or tmp_path / "runtime" / "python",
    )
    profile = _profile()
    ExecutorProfileStore(manager.state_store).save(profile)
    return manager, profile


def _completed(
    command: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_install_requires_existing_paired_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ExecutorServiceManager(
        _settings(tmp_path),
        home=tmp_path / "home",
        environ={},
        system="Linux",
    )
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, **_kwargs: _completed(command),
    )

    with pytest.raises(RuntimeError, match="executor is not paired"):
        manager.install()


def test_systemd_install_is_private_idempotent_and_preserves_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, profile = _paired_manager(
        tmp_path,
        system="Linux",
        oauth_admin_pin="super-private-admin-secret",
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return _completed(command, stdout="active\n")
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    first = manager.install()
    second = manager.install()

    assert first.backend == "systemd"
    assert second.status.state == ExecutorServiceState.RUNNING
    unit = manager.systemd_unit_path.read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    assert str(manager.launcher_path.resolve()) in unit
    assert str(profile.credential) not in unit
    assert profile.control_url not in unit

    service_config = manager.config_path.read_text(encoding="utf-8")
    assert str(profile.credential) not in service_config
    assert profile.control_url not in service_config
    assert "super-private-admin-secret" not in service_config
    assert '"state_dir"' in service_config
    assert '"workspace_root"' in service_config

    launcher = manager.launcher_path.read_text(encoding="utf-8")
    compile(launcher, str(manager.launcher_path), "exec")
    assert str(profile.credential) not in launcher
    assert profile.control_url not in launcher
    assert "workgate.main" in launcher
    assert "--managed-service" in launcher
    assert "traceback.print_exc()" in launcher

    if os.name != "nt":
        assert stat.S_IMODE(manager.config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(manager.launcher_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(manager.systemd_unit_path.stat().st_mode) == 0o600

    saved = ExecutorProfileStore(manager.state_store).load()
    assert saved == profile
    assert (
        sum(
            command[:3] == ["systemctl", "--user", "restart"]
            for command in calls
        )
        == 2
    )


def test_frozen_runtime_uses_managed_service_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)

    command = manager._runtime_command()

    assert command[:4] == [
        str(manager.executable.resolve()),
        "executor",
        "run",
        "--managed-service",
    ]


def test_systemd_status_reports_failed_and_stale_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("systemd", ["/old/python", "/old/launcher.py"])
    manager._write_systemd_unit(["/old/python", "/old/launcher.py"])

    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, **_kwargs: _completed(
            command, returncode=3, stdout="failed\n"
        ),
    )

    status = manager.status()

    assert status.installed is True
    assert status.running is False
    assert status.state == ExecutorServiceState.FAILED
    assert status.runtime_current is False


def test_launchd_definition_uses_private_local_paths_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, profile = _paired_manager(tmp_path, system="Darwin")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/launchctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["launchctl", "print"]:
            return _completed(
                command,
                stdout="state = running\nlast exit code = 0\n",
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    result = manager.install()
    payload = plistlib.loads(manager.launchd_plist_path.read_bytes())

    assert result.status.state == ExecutorServiceState.RUNNING
    assert payload["Label"] == "com.workgate.executor"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ProgramArguments"] == manager._runtime_command()
    assert payload["StandardOutPath"] == str(manager.log_path.resolve())
    rendered = manager.launchd_plist_path.read_text(encoding="utf-8")
    assert str(profile.credential) not in rendered
    assert profile.control_url not in rendered
    assert any(command[:2] == ["launchctl", "bootstrap"] for command in calls)


def test_launchd_status_reports_failed_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Darwin")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("launchd", manager._runtime_command())
    manager._write_launchd_plist(manager._runtime_command())
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/launchctl"
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, **_kwargs: _completed(
            command,
            stdout="state = waiting\nlast exit code = 1\n",
        ),
    )

    status = manager.status()

    assert status.state == ExecutorServiceState.FAILED
    assert status.running is False
    assert "last exit=1" in status.detail


def test_launchd_start_kickstarts_loaded_service_without_rebootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Darwin")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("launchd", manager._runtime_command())
    manager._write_launchd_plist(manager._runtime_command())
    calls: list[list[str]] = []
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/launchctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["launchctl", "print"]:
            return _completed(
                command, stdout="state = running\nlast exit code = 0\n"
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    status = manager.start()

    assert status.state == ExecutorServiceState.RUNNING
    assert any(command[:2] == ["launchctl", "kickstart"] for command in calls)
    assert not any(
        command[:2] == ["launchctl", "bootstrap"] for command in calls
    )


def test_windows_task_is_per_user_persistent_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "runtime" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    pythonw = python.with_name("pythonw.exe")
    pythonw.write_bytes(b"")
    manager, profile = _paired_manager(
        tmp_path,
        system="Windows",
        executable=python,
    )
    registered = False
    running = False
    scripts: list[str] = []

    monkeypatch.setattr(service.shutil, "which", lambda _name: "powershell.exe")

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        nonlocal registered, running
        script = command[-1]
        scripts.append(script)
        if "Register-ScheduledTask" in script:
            registered = True
            return _completed(command)
        if "Start-ScheduledTask" in script:
            running = True
            return _completed(command)
        if "Stop-ScheduledTask" in script:
            running = False
            return _completed(command)
        if "Get-ScheduledTask" in script:
            if not registered:
                return _completed(command, returncode=3)
            payload = {
                "state": "Running" if running else "Ready",
                "last_result": 267009 if running else 0,
            }
            return _completed(command, stdout=json.dumps(payload))
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    result = manager.install()
    registration = next(
        script for script in scripts if "Register-ScheduledTask" in script
    )

    assert result.status.state == ExecutorServiceState.RUNNING
    assert str(pythonw.resolve()) in registration
    assert "New-ScheduledTaskTrigger -AtLogOn" in registration
    assert "-AllowStartIfOnBatteries" in registration
    assert "-DontStopIfGoingOnBatteries" in registration
    assert "-MultipleInstances IgnoreNew" in registration
    assert "-RestartCount 999" in registration
    assert "-RunLevel Limited" in registration
    assert str(profile.credential) not in registration
    assert profile.control_url not in registration


def test_uninstall_preserves_executor_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, profile = _paired_manager(tmp_path, system="Linux")
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return _completed(command, stdout="active\n")
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)
    manager.install()

    status = manager.uninstall()

    assert status.state == ExecutorServiceState.NOT_INSTALLED
    assert ExecutorProfileStore(manager.state_store).load() == profile
    assert not manager.config_path.exists()
    assert not manager.launcher_path.exists()
    assert not manager.metadata_path.exists()


def test_uninstall_keeps_managed_artifacts_when_native_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, profile = _paired_manager(tmp_path, system="Linux")
    fail_disable = False
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        if fail_disable and command[:4] == [
            "systemctl",
            "--user",
            "disable",
            "--now",
        ]:
            raise RuntimeError("disable failed")
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return _completed(command, stdout="active\n")
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)
    manager.install()
    fail_disable = True

    with pytest.raises(RuntimeError, match="disable failed"):
        manager.uninstall()

    assert manager.systemd_unit_path.exists()
    assert manager.metadata_path.exists()
    assert ExecutorProfileStore(manager.state_store).load() == profile


def test_logs_are_bounded_by_line_count_and_read_window(tmp_path: Path) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Darwin")
    manager.service_dir.mkdir(parents=True, exist_ok=True)
    manager.log_path.write_text(
        "\n".join(f"line-{index}" for index in range(1_100)),
        encoding="utf-8",
    )

    output = manager.logs(lines=3)

    assert output.splitlines() == ["line-1097", "line-1098", "line-1099"]
    with pytest.raises(ValueError, match="between 1 and 1000"):
        manager.logs(lines=1_001)
