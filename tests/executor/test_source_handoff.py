"""The source switch must be run by systemd, not by the service it stops."""

import os
import sys
from pathlib import Path

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor import source_handoff
from workgate.executor.service import (
    ExecutorServiceManager,
    UnsupportedExecutorServiceError,
)

VERSION = "5.0.0a77"


@pytest.fixture(autouse=True)
def no_service_config_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("WORKGATE_"):
            monkeypatch.delenv(name)


@pytest.fixture
def prepared(tmp_path: Path):
    if sys.platform != "linux":
        pytest.skip("isolated Linux systemd handoff test")
    checkout = tmp_path / "managed with spaces" / "versions" / ("a" * 40)
    python = checkout / ".venv/bin/python"
    script = checkout / "deploy/source/switch.py"
    config = tmp_path / "executor config.json"
    for path in (python, script):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    config.write_text("{}")
    launcher = checkout.parent.parent / "bin/workgate"
    launcher.parent.mkdir()
    launcher.write_text("old stable launcher, must not change\n")
    return checkout, config, launcher


def test_handoff_uses_transient_user_service_not_executor_cgroup(
    prepared, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, config, launcher = prepared
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))
        return 0, "Running as unit: workgate-executor-source-cutover.service"

    monkeypatch.setattr(source_handoff, "run_bounded_capture", record)
    existing = launcher.read_bytes()
    source_handoff.schedule_linux_source_cutover(
        checkout, version=VERSION, config=config
    )
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        "systemd-run",
        "--user",
        "--unit=workgate-executor-source-cutover",
        "--collect",
        "--no-block",
        "--service-type=exec",
        str(checkout / ".venv/bin/python"),
        str(checkout / "deploy/source/switch.py"),
        "--role",
        "executor",
        "--tag",
        "v" + VERSION,
        "--install-root",
        str(checkout.parent.parent),
        "--executor-config",
        str(config),
    ]
    assert "--scope" not in command
    assert kwargs == {"timeout": 30}
    assert launcher.read_bytes() == existing


@pytest.mark.parametrize(
    "fault",
    [
        "invalid-version",
        "missing-script",
        "symlink-script",
        "missing-interpreter",
        "missing-config",
        "symlink-config",
        "relative-config",
        "environment-override",
    ],
)
def test_invalid_handoff_inputs_fail_without_scheduling(
    prepared, monkeypatch: pytest.MonkeyPatch, fault: str, tmp_path: Path
) -> None:
    checkout, config, _launcher = prepared
    version = VERSION
    if fault == "invalid-version":
        version = "../../../source"
    elif fault in {"missing-script", "symlink-script"}:
        script = checkout / "deploy/source/switch.py"
        script.unlink()
        if fault == "symlink-script":
            destination = tmp_path / "foreign-switch.py"
            destination.write_text("foreign")
            script.symlink_to(destination)
    elif fault == "missing-interpreter":
        (checkout / ".venv/bin/python").unlink()
    elif fault in {"missing-config", "symlink-config"}:
        config.unlink()
        if fault == "symlink-config":
            destination = tmp_path / "foreign config"
            destination.write_text("{}")
            config.symlink_to(destination)
    elif fault == "relative-config":
        config = Path("relative.yaml")
    elif fault == "environment-override":
        monkeypatch.setenv("WORKGATE_OVERRIDE", "no")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("should not invoke systemd-run")

    monkeypatch.setattr(source_handoff, "run_bounded_capture", forbidden)
    with pytest.raises((ValueError, RuntimeError)):
        source_handoff.schedule_linux_source_cutover(
            checkout, version=version, config=config
        )


def test_systemd_launch_failure_is_not_reported_as_success(
    prepared, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, config, launcher = prepared
    original = launcher.read_bytes()
    monkeypatch.setattr(
        source_handoff, "run_bounded_capture", lambda *_a, **_kw: (1, "failed")
    )
    with pytest.raises(RuntimeError, match="could not schedule"):
        source_handoff.schedule_linux_source_cutover(
            checkout, version=VERSION, config=config
        )
    assert launcher.read_bytes() == original


def test_service_manager_rechecks_native_source_owner(
    prepared, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout, _config, _launcher = prepared
    manager = ExecutorServiceManager(
        resolve_executor_config(
            Settings(default_workdir=tmp_path, state_dir=tmp_path / "state")
        ),
        system="Linux",
    )
    captured = []
    monkeypatch.setattr(
        source_handoff,
        "schedule_linux_source_cutover",
        lambda candidate, *, version, config: captured.append(
            (candidate, version, config)
        ),
    )
    monkeypatch.setattr(manager, "verified_source_checkout", lambda: None)
    with pytest.raises(RuntimeError, match="not an owned"):
        manager.schedule_linux_source_cutover(VERSION)
    assert captured == []

    manager.config_path.parent.mkdir(parents=True, exist_ok=True)
    manager.config_path.write_text("{}")
    monkeypatch.setattr(manager, "verified_source_checkout", lambda: checkout)
    manager.schedule_linux_source_cutover(VERSION)
    assert captured == [(checkout, VERSION, manager.config_path)]


def test_windows_cannot_use_linux_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ExecutorServiceManager(
        resolve_executor_config(
            Settings(default_workdir=tmp_path, state_dir=tmp_path / "state")
        ),
        system="Windows",
    )
    monkeypatch.setattr(
        manager,
        "verified_source_checkout",
        lambda: (_ for _ in ()).throw(
            AssertionError("do not query Linux ownership on Windows")
        ),
    )
    with pytest.raises(UnsupportedExecutorServiceError, match="Linux"):
        manager.schedule_linux_source_cutover(VERSION)
