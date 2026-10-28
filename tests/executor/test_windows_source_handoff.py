"""Windows source switch is handed to its own expiring, non-elevated task."""

import os
import subprocess
from pathlib import Path

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor import source_handoff
from workgate.executor.service import (
    ExecutorServiceManager,
    UnsupportedExecutorServiceError,
)
from workgate.platform.host import HostOS
from workgate.platform.windows_task import _literal
from workgate.supervisor import PARENT_FD_ENV

VERSION = "5.0.0a77"


@pytest.fixture(autouse=True)
def windows_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_handoff, "host_os", lambda: HostOS.WINDOWS)
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda name: "powershell.exe",
    )
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)


@pytest.fixture
def prepared(tmp_path: Path):
    checkout = tmp_path / "managed source" / "versions" / ("a" * 40)
    pythonw = checkout / ".venv" / "Scripts" / "pythonw.exe"
    script = checkout / "deploy" / "source" / "switch_windows.py"
    config = tmp_path / "O'Brien config.json"
    for file in (pythonw, script):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture")
    config.write_text("{}")
    launcher = checkout.parent.parent / "bin" / "workgate.cmd"
    launcher.parent.mkdir()
    launcher.write_text("old launcher must stay unchanged\n")
    return checkout, config, launcher


def test_windows_handoff_uses_separate_one_time_task(
    prepared, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, config, launcher = prepared
    existing = launcher.read_bytes()
    # WorkGate's own supervised child receives this internal environment key.
    monkeypatch.setenv(PARENT_FD_ENV, "123")
    commands = []

    def capture(argv: list[str]):
        commands.append(argv)
        # The first status lookup reports that no owned one-off task exists.
        return subprocess.CompletedProcess(argv, 0, "null", "")

    source_handoff.schedule_windows_source_cutover(
        checkout, version=VERSION, config=config, run=capture
    )
    assert len(commands) == 2
    script = commands[-1][-1]
    assert "Register-ScheduledTask" in script
    assert "New-ScheduledTaskTrigger -Once" in script
    assert "Start-ScheduledTask" not in script  # Avoid a second trigger run.
    assert "-StartWhenAvailable" in script
    assert "AddSeconds(15)" in script
    assert "Workgate Executor Source Cutover" in script
    assert "Workgate Executor', " not in script
    assert "Workgate Executor Source Cutover" != "Workgate Executor"
    assert "-LogonType Interactive -RunLevel Limited" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-DeleteExpiredTaskAfter" in script
    assert "-ExecutionTimeLimit" in script
    assert "EndBoundary" in script
    assert "-AtLogOn" not in script
    assert "-Force" not in script
    assert "-Action $action" in script
    assert _literal(str(checkout / ".venv/Scripts/pythonw.exe")) in script
    args = subprocess.list2cmdline(
        [
            str(checkout / "deploy/source/switch_windows.py"),
            "--role",
            "executor",
            "--tag",
            "v" + VERSION,
            "--install-root",
            str(checkout.parent.parent),
            "--executor-config",
            str(config),
        ]
    )
    assert _literal(args) in script
    assert "O''Brien" in script
    assert launcher.read_bytes() == existing


@pytest.mark.parametrize(
    "fault",
    [
        "invalid-version",
        "no-script",
        "symlink-script",
        "no-pythonw",
        "symlink-pythonw",
        "no-config",
        "symlink-config",
        "relative-config",
        "environment-override",
    ],
)
def test_invalid_handoff_never_registers_task(
    prepared, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    checkout, config, _launcher = prepared
    version = VERSION
    if fault == "invalid-version":
        version = "../arbitrary"
    elif fault in {"no-script", "symlink-script"}:
        script = checkout / "deploy/source/switch_windows.py"
        script.unlink()
        if fault == "symlink-script":
            path = tmp_path / "wrong switch"
            path.write_text("unknown code")
            script.symlink_to(path)
    elif fault in {"no-pythonw", "symlink-pythonw"}:
        pythonw = checkout / ".venv/Scripts/pythonw.exe"
        pythonw.unlink()
        if fault == "symlink-pythonw":
            path = tmp_path / "unmanaged pythonw"
            path.write_text("unknown code")
            pythonw.symlink_to(path)
    elif fault in {"no-config", "symlink-config"}:
        config.unlink()
        if fault == "symlink-config":
            path = tmp_path / "different config"
            path.write_text("{}")
            config.symlink_to(path)
    elif fault == "relative-config":
        config = Path("relative.json")
    else:
        monkeypatch.setenv(PARENT_FD_ENV, "123")
        monkeypatch.setenv("WORKGATE_OVERRIDE", "unapproved")

    def forbidden(_argv):
        raise AssertionError("must not contact Task Scheduler")

    with pytest.raises((RuntimeError, ValueError)):
        source_handoff.schedule_windows_source_cutover(
            checkout, version=version, config=config, run=forbidden
        )


def test_scheduler_does_not_replace_existing_one_shot_task(
    prepared,
) -> None:
    checkout, config, _launcher = prepared
    commands = []

    def run(argv: list[str]):
        commands.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, '{"state":"Running","last_result":0}', ""
        )

    with pytest.raises(RuntimeError, match="already registered"):
        source_handoff.schedule_windows_source_cutover(
            checkout, version=VERSION, config=config, run=run
        )
    assert len(commands) == 1
    assert "Register-ScheduledTask" not in commands[0][-1]


def test_scheduler_creation_failure_propagates(
    prepared,
) -> None:
    checkout, config, launcher = prepared
    original = launcher.read_bytes()
    calls = []

    def run(argv: list[str]):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, "null", "")
        raise subprocess.CalledProcessError(1, argv)

    with pytest.raises(subprocess.CalledProcessError):
        source_handoff.schedule_windows_source_cutover(
            checkout, version=VERSION, config=config, run=run
        )
    assert len(calls) == 2
    assert launcher.read_bytes() == original


def test_owned_service_manager_verifies_source_before_scheduling(
    prepared, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, _config, _launcher = prepared
    manager = ExecutorServiceManager(
        resolve_executor_config(
            Settings(default_workdir=tmp_path, state_dir=tmp_path / "state")
        ),
        system="Windows",
    )
    candidates = []
    monkeypatch.setattr(manager, "verified_source_checkout", lambda: None)
    monkeypatch.setattr(
        source_handoff,
        "schedule_windows_source_cutover",
        lambda *args, **kwargs: candidates.append((args, kwargs)),
    )
    with pytest.raises(RuntimeError, match="not an owned"):
        manager.schedule_windows_source_cutover(VERSION)
    assert candidates == []

    monkeypatch.setattr(manager, "verified_source_checkout", lambda: checkout)
    manager.schedule_windows_source_cutover(VERSION)
    assert candidates[0][0] == (checkout,)
    assert candidates[0][1]["config"] == manager.config_path
    assert candidates[0][1]["run"] == manager._run


def test_linux_manager_cannot_schedule_windows_task(tmp_path: Path) -> None:
    manager = ExecutorServiceManager(
        resolve_executor_config(
            Settings(default_workdir=tmp_path, state_dir=tmp_path / "state")
        ),
        system="Linux",
    )
    with pytest.raises(UnsupportedExecutorServiceError, match="Windows"):
        manager.schedule_windows_source_cutover(VERSION)
