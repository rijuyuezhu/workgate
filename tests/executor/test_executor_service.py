import json
import os
import plistlib
import shlex
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from workgate.config.executor import ExecutorConfig, resolve_executor_config
from workgate.config.settings import Settings, load_settings
from workgate.executor import runtime_update, service
from workgate.executor.profile import ExecutorProfile, ExecutorProfileStore
from workgate.executor.runtime_update import ExecutorRuntimeStateStore
from workgate.executor.service import (
    ExecutorServiceManager,
    ExecutorServiceState,
    ExecutorServiceStatus,
)
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.executor import ExecutorRuntimeOwnership
from workgate.protocol.ids import new_executor_id
from workgate.utils.systemd import systemd_quote


@pytest.fixture(autouse=True)
def _portable_posix_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(service.os, "getuid"):
        monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)


def _settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(
        default_workdir=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
        **kwargs,
    )


def _config(tmp_path: Path, **kwargs) -> ExecutorConfig:
    return resolve_executor_config(_settings(tmp_path, **kwargs))


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
        _config(tmp_path, **settings_kwargs),
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


def test_managed_executor_launcher_scopes_crypto_legacy_to_windows_arm64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Windows")
    monkeypatch.delenv("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", raising=False)
    source = manager._launcher_source(ExecutorRuntimeOwnership.SOURCE)
    compile(source, "<generated-executor-launcher>", "exec")
    assert "sysconfig.get_platform() == 'win-arm64'" in source
    assert "os.environ['CRYPTOGRAPHY_OPENSSL_NO_LEGACY'] = '1'" in source
    assert source.index("CRYPTOGRAPHY_OPENSSL_NO_LEGACY") < source.index(
        "from workgate.main import main"
    )
    assert "CRYPTOGRAPHY_OPENSSL_NO_LEGACY" not in os.environ


def test_service_helpers_cover_nested_settings_and_command_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "example"
    child = tmp_path / "child"
    nested = (parent, {"paths": [child]})
    assert service._json_value(nested) == [
        str(parent),
        {"paths": [str(child)]},
    ]


@pytest.mark.parametrize(
    ("system", "message"),
    [
        ("Linux", "systemd --user is required"),
        ("Darwin", "launchctl is required"),
        ("Haiku", "unsupported on Haiku"),
    ],
)
def test_backend_reports_unavailable_native_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    message: str,
) -> None:
    manager = ExecutorServiceManager(
        _config(tmp_path),
        home=tmp_path / "home",
        environ={},
        system=system,
    )
    monkeypatch.setattr(service.shutil, "which", lambda _name: None)

    with pytest.raises(service.UnsupportedExecutorServiceError, match=message):
        manager.backend(require_available=True)


def test_linux_backend_reports_unavailable_user_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ExecutorServiceManager(
        _config(tmp_path),
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
        lambda command, **_kwargs: _completed(
            command, returncode=1, stderr="no user manager"
        ),
    )

    with pytest.raises(
        service.UnsupportedExecutorServiceError,
        match="systemd user manager is unavailable",
    ):
        manager.backend(require_available=True)


def test_managed_config_reconstructs_foreground_executor_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(
        tmp_path,
        system="Linux",
        max_output_bytes=123_456,
    )
    manager._write_service_config()
    for name in tuple(os.environ):
        if name.startswith("WORKGATE_"):
            monkeypatch.delenv(name)

    loaded = load_settings(manager.config_path)

    assert resolve_executor_config(loaded) == manager.config


def test_install_requires_existing_paired_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ExecutorServiceManager(
        _config(tmp_path),
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


def test_failed_service_install_does_not_grant_control_managed_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "runtime" / "workgate"
    manager, _profile_value = _paired_manager(
        tmp_path,
        system="Linux",
        executable=executable,
    )
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(runtime_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_update.sys, "executable", str(executable))
    unit_dir = tmp_path / "manager-config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)

    def fail_restart(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["systemctl", "--user", "restart"]:
            raise RuntimeError("restart failed")
        if command == [
            "systemctl",
            "--user",
            "show",
            "--property=UnitPath",
            "--value",
        ]:
            return _completed(
                command, stdout=f'"{unit_dir}" /etc/systemd/user\n'
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fail_restart)

    with pytest.raises(RuntimeError, match="restart failed"):
        manager.install(
            runtime_ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED
        )

    assert ExecutorRuntimeStateStore(manager.state_store).load() is None


def test_service_install_preserves_update_state_written_during_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "runtime" / "workgate"
    manager, _profile_value = _paired_manager(
        tmp_path,
        system="Linux",
        executable=executable,
    )
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(runtime_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_update.sys, "executable", str(executable))
    unit_dir = tmp_path / "manager-config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    runtime_store = ExecutorRuntimeStateStore(manager.state_store)

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        if command == [
            "systemctl",
            "--user",
            "show",
            "--property=UnitPath",
            "--value",
        ]:
            return _completed(
                command, stdout=f'"{unit_dir}" /etc/systemd/user\n'
            )
        if command[:3] == ["systemctl", "--user", "restart"]:
            runtime_store.record_install(
                ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED
            )
            runtime_store.set_update(
                runtime_update.ExecutorRuntimeUpdateStatus.PENDING,
                target_version="next-version",
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: ExecutorServiceStatus(
            backend="systemd",
            state=ExecutorServiceState.RUNNING,
            installed=True,
            running=True,
        ),
    )

    manager.install(runtime_ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)

    state = runtime_store.load()
    assert state is not None
    assert state.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
    assert (
        state.update_status
        is runtime_update.ExecutorRuntimeUpdateStatus.PENDING
    )
    assert state.update_target_version == "next-version"


def test_systemd_install_is_private_idempotent_and_preserves_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, profile = _paired_manager(
        tmp_path,
        system="Linux",
        oauth_admin_pin="super-private-admin-secret",
    )
    manager.environ.update(
        {
            "PATH": "/custom$bin%dir:relative:/usr/local/bin",
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        }
    )
    calls: list[list[str]] = []
    manager_unit_dir = tmp_path / "manager config" / "systemd" / "user"
    manager_unit_path = manager_unit_dir / "workgate-executor.service"

    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command == [
            "systemctl",
            "--user",
            "show",
            "--property=UnitPath",
            "--value",
        ]:
            return _completed(
                command,
                stdout=f'"{manager_unit_dir}" /etc/systemd/user\n',
            )
        if command[:3] == ["systemctl", "--user", "show"]:
            return _completed(
                command,
                stdout=(
                    "LoadState=loaded\n"
                    "ActiveState=active\n"
                    "SubState=running\n"
                    f"FragmentPath={manager_unit_path}\n"
                ),
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    first = manager.install()
    second = manager.install()

    assert first.backend == "systemd"
    assert second.state == ExecutorServiceState.RUNNING
    assert manager.systemd_unit_path == manager_unit_path
    assert not (tmp_path / "xdg-config" / "systemd" / "user").exists()
    metadata = json.loads(manager.metadata_path.read_text(encoding="utf-8"))
    assert metadata["service_file"] == str(manager_unit_path)
    unit = manager.systemd_unit_path.read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    assert systemd_quote(str(manager.launcher_path.resolve())) in unit
    encoded_custom_path = "/custom$bin%%dir"
    assert encoded_custom_path in unit
    assert "custom$$bin" not in unit
    assert "relative" not in unit
    assert "XDG_CONFIG_HOME" not in unit
    assert str(profile.credential) not in unit
    assert profile.control_url not in unit

    changed_shell = ExecutorServiceManager(
        manager.config,
        home=manager.home,
        environ={"XDG_CONFIG_HOME": str(tmp_path / "different-shell-xdg")},
        system="Linux",
        executable=manager.executable,
    )
    assert changed_shell.systemd_unit_path == manager_unit_path

    service_config = manager.config_path.read_text(encoding="utf-8")
    assert str(profile.credential) not in service_config
    assert profile.control_url not in service_config
    assert "super-private-admin-secret" not in service_config
    assert '"state_dir"' in service_config
    assert '"default_workdir"' in service_config

    launcher = manager.launcher_path.read_text(encoding="utf-8")
    compile(launcher, str(manager.launcher_path), "exec")
    assert str(profile.credential) not in launcher
    assert profile.control_url not in launcher
    assert "workgate.main" in launcher
    assert "--managed-service" in launcher
    assert "--runtime-ownership" in launcher
    assert "'source'" in launcher

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


def test_systemd_failed_reload_keeps_previous_runtime_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    previous_command = ["/previous/python", "/previous/launcher.py"]
    unit_dir = manager.home / ".config" / "systemd" / "user"
    unit_path = unit_dir / "workgate-executor.service"
    manager._write_metadata(
        "systemd",
        previous_command,
        service_file=unit_path,
    )
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        if command == ["systemctl", "--user", "show-environment"]:
            return _completed(command)
        if command == [
            "systemctl",
            "--user",
            "show",
            "--property=UnitPath",
            "--value",
        ]:
            return _completed(command, stdout=shlex.quote(str(unit_dir)) + "\n")
        if command == ["systemctl", "--user", "daemon-reload"]:
            raise RuntimeError("reload failed")
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    with pytest.raises(RuntimeError, match="reload failed"):
        manager.install()

    metadata = json.loads(manager.metadata_path.read_text(encoding="utf-8"))
    assert metadata["command"] == previous_command
    assert manager._runtime_current() is False


def test_frozen_runtime_uses_managed_service_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)

    command = manager._runtime_command()

    assert command[:4] == [
        str(manager.executable.absolute()),
        "executor",
        "run",
        "--managed-service",
    ]
    assert command[4:6] == ["--runtime-ownership", "self-contained"]


def test_runtime_command_preserves_virtualenv_interpreter_symlink(
    tmp_path: Path,
) -> None:
    real_python = tmp_path / "python-real"
    real_python.write_bytes(b"")
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(real_python)
    manager, _profile_value = _paired_manager(
        tmp_path,
        system="Linux",
        executable=venv_python,
    )

    command = manager._runtime_command()

    assert command[0] == str(venv_python.absolute())
    assert command[0] != str(venv_python.resolve())


def test_systemd_metadata_rejects_unsafe_service_path(tmp_path: Path) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    command = manager._runtime_command()
    unsafe = tmp_path / "important.txt"
    unsafe.write_text("keep me", encoding="utf-8")
    manager._write_metadata(
        "systemd",
        command,
        service_file=unsafe,
    )

    assert manager._metadata_service_file("systemd") is None
    assert manager.systemd_unit_path == (
        manager.home
        / ".config"
        / "systemd"
        / "user"
        / "workgate-executor.service"
    )
    assert unsafe.read_text(encoding="utf-8") == "keep me"


def test_runtime_current_rejects_metadata_for_different_backend(
    tmp_path: Path,
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    manager._write_metadata("launchd", manager._runtime_command())

    assert manager._runtime_current() is False


def test_runtime_current_rejects_metadata_from_previous_workgate_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("systemd", manager._runtime_command())
    assert manager._runtime_current() is True

    monkeypatch.setattr(service, "__version__", "999.0-test")

    assert manager._runtime_current() is False


def test_runtime_current_tracks_managed_config_snapshot(tmp_path: Path) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("systemd", manager._runtime_command())
    assert manager._runtime_current() is True

    installed = json.loads(manager.config_path.read_text(encoding="utf-8"))
    installed["max_output_bytes"] += 1
    manager.config_path.write_text(json.dumps(installed), encoding="utf-8")
    assert manager._runtime_current() is False

    manager._write_service_config()
    manager.config = replace(
        manager.config, max_output_bytes=manager.config.max_output_bytes + 1
    )
    assert manager._runtime_current() is False

    manager.config = _config(tmp_path)
    manager.config_path.unlink()
    assert manager._runtime_current() is False


def test_runtime_current_rejects_missing_launcher(tmp_path: Path) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("systemd", manager._runtime_command())
    assert manager._runtime_current() is True

    manager.launcher_path.unlink()

    assert manager._runtime_current() is False


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


def test_status_does_not_reclassify_control_managed_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "runtime" / "workgate"
    executable.parent.mkdir(parents=True)
    executable.write_text("runtime")
    manager, _profile_value = _paired_manager(
        tmp_path, system="Linux", executable=executable
    )
    manager._write_service_config()
    manager._write_launcher(manager._runtime_command())
    service_file = manager._write_systemd_unit(manager._runtime_command())
    manager._write_metadata(
        "systemd", manager._runtime_command(), service_file=service_file
    )

    monkeypatch.setattr(runtime_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_update.sys, "executable", str(executable))
    state_store = ExecutorRuntimeStateStore(manager.state_store)
    state_store.record_install(
        ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED
    )

    monkeypatch.delattr(runtime_update.sys, "frozen", raising=False)
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, **_kwargs: _completed(
            command,
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                f"FragmentPath={service_file}\n"
            ),
        ),
    )

    status = manager.status()

    assert status.runtime_ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
    persisted = state_store.load()
    assert persisted is not None
    assert persisted.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
    assert persisted.executable == str(executable.absolute())


def test_systemd_status_rejects_different_loaded_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    command = manager._runtime_command()
    expected_unit = manager.systemd_unit_path
    manager._write_service_config()
    manager._write_launcher(command)
    manager._write_systemd_unit(command, path=expected_unit)
    manager._write_metadata(
        "systemd",
        command,
        service_file=expected_unit,
    )
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, **_kwargs: _completed(
            command,
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                f"FragmentPath={tmp_path / 'other' / 'workgate-executor.service'}\n"
            ),
        ),
    )

    status = manager.status()

    assert status.state == ExecutorServiceState.FAILED
    assert status.running is True
    assert status.runtime_current is False
    assert "loaded a different definition" in status.detail


def test_systemd_status_and_uninstall_recover_loaded_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, profile = _paired_manager(tmp_path, system="Linux")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("systemd", manager._runtime_command())
    runtime_store = ExecutorRuntimeStateStore(manager.state_store)
    runtime_store.record_install(ownership=ExecutorRuntimeOwnership.SOURCE)
    loaded = True
    active = True
    calls: list[list[str]] = []
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        nonlocal loaded, active
        calls.append(command)
        if command[:3] == ["systemctl", "--user", "show"]:
            return _completed(
                command,
                stdout=(
                    f"LoadState={'loaded' if loaded else 'not-found'}\n"
                    f"ActiveState={'active' if active else 'inactive'}\n"
                    f"SubState={'running' if active else 'dead'}\n"
                ),
            )
        if command[:3] == ["systemctl", "--user", "stop"]:
            active = False
        if (
            command[:3] == ["systemctl", "--user", "daemon-reload"]
            and not manager.systemd_unit_path.exists()
        ):
            loaded = False
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    status = manager.status()
    assert status.installed is True
    assert status.state == ExecutorServiceState.RUNNING
    assert status.runtime_current is False
    assert "definition missing" in status.detail

    removed = manager.uninstall()

    assert removed.state == ExecutorServiceState.NOT_INSTALLED
    assert ExecutorProfileStore(manager.state_store).load() == profile
    assert runtime_store.load() is None
    assert any(
        command[:3] == ["systemctl", "--user", "stop"] for command in calls
    )


def test_systemd_uninstall_recovers_custom_unit_path_from_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    manager._write_service_config()
    manager._write_launcher()
    custom_unit_dir = tmp_path / "custom config" / "systemd" / "user"
    custom_unit_path = custom_unit_dir / "workgate-executor.service"
    custom_unit_dir.mkdir(parents=True)
    custom_unit_path.write_text(
        "[Service]\nExecStart=/bin/false\n", encoding="utf-8"
    )
    loaded = True
    calls: list[list[str]] = []
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        nonlocal loaded
        calls.append(command)
        if command == [
            "systemctl",
            "--user",
            "show",
            "--property=UnitPath",
            "--value",
        ]:
            return _completed(
                command, stdout=f'"{custom_unit_dir}" /usr/lib/systemd/user\n'
            )
        if command[:3] == ["systemctl", "--user", "show"]:
            return _completed(
                command,
                stdout=(
                    f"LoadState={'loaded' if loaded else 'not-found'}\n"
                    f"ActiveState={'active' if loaded else 'inactive'}\n"
                    f"SubState={'running' if loaded else 'dead'}\n"
                    f"FragmentPath={custom_unit_path if loaded else ''}\n"
                ),
            )
        if command[:4] == ["systemctl", "--user", "disable", "--now"]:
            loaded = False
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    removed = manager.uninstall()

    assert removed.state == ExecutorServiceState.NOT_INSTALLED
    assert not custom_unit_path.exists()
    assert any(
        command[:4] == ["systemctl", "--user", "disable", "--now"]
        for command in calls
    )


def test_launchd_definition_uses_private_local_paths_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, profile = _paired_manager(tmp_path, system="Darwin")
    manager.environ["PATH"] = "/custom-bin:relative:/usr/local/bin"
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

    status = manager.install()
    payload = plistlib.loads(manager.launchd_plist_path.read_bytes())

    assert status.state == ExecutorServiceState.RUNNING
    assert payload["Label"] == "com.workgate.executor"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ProgramArguments"] == manager._runtime_command()
    launchd_environment = payload["EnvironmentVariables"]
    launchd_path = launchd_environment["PATH"].split(":")
    assert launchd_path[0] == "/custom-bin"
    assert "/custom-bin" in launchd_path
    assert "relative" not in launchd_path
    assert "/opt/homebrew/bin" in launchd_path
    assert launchd_environment["PYTHONUNBUFFERED"] == "1"
    assert payload["StandardOutPath"] == str(manager.log_path.resolve())
    rendered = manager.launchd_plist_path.read_text(encoding="utf-8")
    assert str(profile.credential) not in rendered
    assert profile.control_url not in rendered
    assert any(command[:2] == ["launchctl", "bootstrap"] for command in calls)


@pytest.mark.skipif(
    service.platform.system() != "Darwin",
    reason="requires macOS plutil",
)
def test_launchd_plist_passes_native_plutil_lint(tmp_path: Path) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Darwin")
    command = manager._runtime_command()
    manager._write_service_config()
    manager._write_launcher(command)
    plist_path = manager._write_launchd_plist(command)

    result = subprocess.run(
        ["plutil", "-lint", str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_launchd_failed_bootstrap_keeps_previous_runtime_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Darwin")
    previous_command = ["/previous/python", "/previous/launcher.py"]
    manager._write_metadata(
        "launchd",
        previous_command,
        service_file=manager.launchd_plist_path,
    )
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/launchctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["launchctl", "bootstrap"]:
            raise RuntimeError("bootstrap failed")
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        manager.install()

    metadata = json.loads(manager.metadata_path.read_text(encoding="utf-8"))
    assert metadata["command"] == previous_command
    assert manager._runtime_current() is False


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


def test_launchd_stop_fails_if_bootout_does_not_unload_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Darwin")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_launchd_plist(manager._runtime_command())
    manager._write_metadata(
        "launchd",
        manager._runtime_command(),
        service_file=manager.launchd_plist_path,
    )
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/launchctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["launchctl", "bootout"]:
            return _completed(command, returncode=5, stderr="bootout failed")
        if command[:2] == ["launchctl", "print"]:
            return _completed(
                command,
                stdout="state = running\nlast exit code = 0\n",
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    with pytest.raises(
        RuntimeError, match="failed to stop executor LaunchAgent"
    ):
        manager.stop()


def test_launchd_status_and_uninstall_recover_loaded_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, profile = _paired_manager(tmp_path, system="Darwin")
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("launchd", manager._runtime_command())
    loaded = True
    calls: list[list[str]] = []
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/launchctl"
    )

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        nonlocal loaded
        calls.append(command)
        if command[:2] == ["launchctl", "print"]:
            if loaded:
                return _completed(
                    command,
                    stdout="state = running\nlast exit code = 0\n",
                )
            return _completed(command, returncode=3, stderr="not found")
        if command[:2] == ["launchctl", "bootout"]:
            loaded = False
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)

    status = manager.status()
    assert status.installed is True
    assert status.state == ExecutorServiceState.RUNNING
    assert status.runtime_current is False
    assert "definition missing" in status.detail

    removed = manager.uninstall()

    assert removed.state == ExecutorServiceState.NOT_INSTALLED
    assert ExecutorProfileStore(manager.state_store).load() == profile
    assert any(
        command == ["launchctl", "bootout", manager._launchd_service_target]
        for command in calls
    )


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
        if command == [
            "systemctl",
            "--user",
            "show",
            "--property=UnitPath",
            "--value",
        ]:
            unit_dir = manager.home / ".config" / "systemd" / "user"
            return _completed(
                command,
                stdout=shlex.quote(str(unit_dir)) + "\n",
            )
        if command[:3] == ["systemctl", "--user", "show"]:
            if manager.systemd_unit_path.exists():
                return _completed(
                    command,
                    stdout=(
                        "LoadState=loaded\n"
                        "ActiveState=active\n"
                        "SubState=running\n"
                    ),
                )
            return _completed(
                command,
                stdout=(
                    "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
                ),
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)
    manager.install()

    status = manager.uninstall()

    assert status.state == ExecutorServiceState.NOT_INSTALLED
    assert status.runtime_current is False
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
        if command == [
            "systemctl",
            "--user",
            "show",
            "--property=UnitPath",
            "--value",
        ]:
            unit_dir = manager.home / ".config" / "systemd" / "user"
            return _completed(
                command,
                stdout=shlex.quote(str(unit_dir)) + "\n",
            )
        if fail_disable and command[:4] == [
            "systemctl",
            "--user",
            "disable",
            "--now",
        ]:
            raise RuntimeError("disable failed")
        if command[:3] == ["systemctl", "--user", "show"]:
            return _completed(
                command,
                stdout=(
                    "LoadState=loaded\nActiveState=active\nSubState=running\n"
                ),
            )
        return _completed(command)

    monkeypatch.setattr(manager, "_run", fake_run)
    manager.install()
    fail_disable = True

    with pytest.raises(RuntimeError, match="disable failed"):
        manager.uninstall()

    assert manager.systemd_unit_path.exists()
    assert manager.metadata_path.exists()
    assert ExecutorProfileStore(manager.state_store).load() == profile


def test_systemd_logs_use_journal_even_when_definition_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile_value = _paired_manager(tmp_path, system="Linux")
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _completed(command, stdout="recent executor log\n")

    monkeypatch.setattr(manager, "_run", fake_run)

    output = manager.logs(lines=10)

    assert output == "recent executor log\n"
    assert calls[0][:4] == [
        "journalctl",
        "--user",
        "-u",
        "workgate-executor.service",
    ]


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


@pytest.mark.parametrize(
    "native_command,expected_current",
    [
        ("exact", True),
        ("wrong-python", False),
        ("wrong-launcher", False),
        ("extra-argument", False),
        ("extra-execstart", False),
        ("missing", False),
    ],
)
def test_systemd_status_checks_effective_execstart_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_command,
    expected_current,
) -> None:
    manager, _profile = _paired_manager(tmp_path, system="Linux")
    manager._write_service_config()
    command = manager._runtime_command()
    manager._write_launcher(command)
    unit = manager._write_systemd_unit(command)
    manager._write_metadata("systemd", command, service_file=unit)
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )

    args = list(command)
    if native_command == "wrong-python":
        args[0] = "/other/python"
    elif native_command == "wrong-launcher":
        args[1] = "/other/launcher.py"
    elif native_command == "extra-argument":
        args.append("--unexpected")

    serialized = (
        f"{{ path={args[0]} ; argv[]={' '.join(args)} ; ignore_errors=no ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )
    if native_command == "missing":
        serialized = ""
    elif native_command == "extra-execstart":
        serialized += (
            " { path=/bin/false ; argv[]=/bin/false ; ignore_errors=no ; }"
        )

    def fake_run(arguments, **_kwargs):
        if arguments[:3] == ["systemctl", "--user", "show"]:
            return _completed(
                arguments,
                stdout=(
                    "LoadState=loaded\n"
                    "ActiveState=active\n"
                    "SubState=running\n"
                    f"FragmentPath={unit}\n"
                    + (f"ExecStart={serialized}\n" if serialized else "")
                ),
            )
        return _completed(arguments)

    monkeypatch.setattr(manager, "_run", fake_run)
    status = manager.status()
    assert status.state == ExecutorServiceState.RUNNING
    assert status.installed is True
    assert status.runtime_current is expected_current


@pytest.mark.parametrize(
    "native_load,fragment",
    [
        ("not-found", "same"),
        ("loaded", "missing"),
        ("loaded", "other"),
    ],
)
def test_systemd_status_requires_correct_loaded_fragment_for_current_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_load, fragment
) -> None:
    manager, _profile = _paired_manager(tmp_path, system="Linux")
    manager._write_service_config()
    cmd = manager._runtime_command()
    manager._write_launcher(cmd)
    unit = manager._write_systemd_unit(cmd)
    manager._write_metadata("systemd", cmd, service_file=unit)
    monkeypatch.setattr(
        service.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    path = {
        "same": str(unit),
        "missing": "",
        "other": str(tmp_path / "other" / "workgate-executor.service"),
    }[fragment]

    def fake_run(arguments, **_kwargs):
        if arguments[:3] == ["systemctl", "--user", "show"]:
            return _completed(
                arguments,
                stdout=(
                    f"LoadState={native_load}\n"
                    "ActiveState=active\n"
                    "SubState=running\n"
                    f"FragmentPath={path}\n"
                    f"ExecStart={{ path={cmd[0]} ; argv[]={' '.join(cmd)} ; "
                    "ignore_errors=no ; start_time=[n/a] ; }}\n"
                ),
            )
        return _completed(arguments)

    monkeypatch.setattr(manager, "_run", fake_run)
    assert not manager.status().runtime_current
