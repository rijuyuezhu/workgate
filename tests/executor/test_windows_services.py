"""Windows user-service contracts without changing the host's scheduled tasks."""

import ast
import json
import os
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.executor.test_executor_service import _paired_manager
from workgate.colocate import service as colocate_service
from workgate.executor import service as executor_service
from workgate.executor.profile import ExecutorProfile, ExecutorProfileStore
from workgate.persistence import FileStateStore
from workgate.platform.windows_task import WindowsUserTask, _literal
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.ids import new_executor_id


def test_windows_task_quotes_arguments_and_limits_identity(monkeypatch):
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "null", "")

    task = WindowsUserTask(
        "Test Workgate", description="Managed by Workgate: test", run=run
    )
    task.install(
        [r"C:\Program Files\python.exe", r"C:\O'Brien 中文\run.py"],
        cwd=r"C:\Test",
    )
    script = calls[-1][-1]
    assert "-RunLevel Limited" in script
    assert "-LogonType Interactive" in script
    assert "-AtLogOn -User $user" in script
    assert "-RestartCount 3" in script
    assert _literal(r"C:\Program Files\python.exe") in script
    assert "O''Brien" in script
    assert "-Argument '" in script
    assert not any("cmd.exe" in arg.lower() for arg in calls[-1])


@pytest.mark.parametrize(
    "state,expected",
    [
        ("Running", "running"),
        ("Ready", "stopped"),
        ("Disabled", "failed"),
    ],
)
def test_windows_executor_status_preserves_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, expected: str
):
    manager, _ = _paired_manager(tmp_path, system="Windows")
    monkeypatch.setattr(
        manager,
        "_windows_task",
        lambda: SimpleNamespace(
            status=lambda: {"state": state, "last_result": 0},
        ),
    )
    status = manager.status()
    assert status.state.value == expected
    assert status.backend == "scheduled-task"
    assert status.installed


def test_windows_executor_install_uninstall_retains_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager, profile = _paired_manager(tmp_path, system="Windows")
    commands: list[tuple[str, object]] = []
    active = False
    installed = False

    class Task:
        def status(self):
            return (
                {"state": "Running" if active else "Ready", "last_result": 0}
                if installed
                else None
            )

        def install(self, command, *, cwd):
            nonlocal installed
            installed = True
            commands.append(("install", (command, cwd)))

        def start(self):
            nonlocal active
            active = True
            commands.append(("start", None))

        def cancel_runtime_restart(self):
            commands.append(("cancel-restart", None))

        def stop(self):
            nonlocal active
            active = False
            commands.append(("stop", None))

        def uninstall(self):
            nonlocal installed
            installed = False
            commands.append(("uninstall", None))

    monkeypatch.setattr(manager, "_windows_task", Task)
    monkeypatch.setattr(
        executor_service.WindowsUserTask, "available", lambda: True
    )
    pythonw = manager.executable.with_name("pythonw.exe")
    pythonw.parent.mkdir(parents=True, exist_ok=True)
    pythonw.touch()
    status = manager.install()
    assert status.state.value == "running"
    assert manager.config_path.is_file()
    assert manager.launcher_path.is_file()
    assert manager.metadata_path.is_file()
    assert commands[0][0] == "install"
    manager.restart()
    assert [item[0] for item in commands[-3:]] == [
        "cancel-restart",
        "stop",
        "start",
    ]
    manager.stop()
    assert manager.status().state.value == "stopped"
    manager.start()
    assert manager.status().state.value == "running"
    assert manager.uninstall().state.value == "not-installed"
    assert not manager.config_path.exists()
    assert manager.state_store is not None
    from workgate.executor.profile import ExecutorProfileStore

    assert ExecutorProfileStore(manager.state_store).load() == profile


def test_colocate_windows_lifecycle_reuses_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(colocate_service.sys, "platform", "win32")
    monkeypatch.setattr(
        colocate_service.WindowsUserTask, "available", lambda: True
    )
    monkeypatch.setattr(
        colocate_service, "_windows_service_dir", lambda: tmp_path
    )
    calls = []

    class Task:
        def status(self):
            return {"state": "Ready", "last_result": 0}

        def start(self):
            calls.append("start")

        def stop(self):
            calls.append("stop")

        def uninstall(self):
            calls.append("uninstall")

    monkeypatch.setattr(colocate_service, "_windows_task", Task)
    colocate_service.manage_colocate_service("restart")
    assert calls == ["stop", "start"]
    colocate_service.manage_colocate_service("uninstall")
    assert calls[-1] == "uninstall"


def _paired_colocate_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    control_config = tmp_path / "control.yaml"
    executor_config = tmp_path / "executor.yaml"
    executor_state = tmp_path / "executor-state"
    control_config.write_text("mode: mcp\n")
    executor_config.write_text(f"state_dir: {executor_state}\n")
    ExecutorProfileStore(FileStateStore(lambda: executor_state)).save(
        ExecutorProfile(
            control_url="http://127.0.0.1:9000",
            executor_id=new_executor_id(),
            credential=new_executor_credential(),
        )
    )
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(colocate_service.sys, "platform", "win32")
    monkeypatch.setattr(
        colocate_service.WindowsUserTask, "available", lambda: True
    )
    python = tmp_path / "runtime" / "python.exe"
    monkeypatch.setattr(colocate_service.sys, "executable", str(python))
    return Namespace(
        control_config=str(control_config), executor_config=str(executor_config)
    ), python


def test_windows_colocate_install_uses_direct_pythonw_with_role_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args, python = _paired_colocate_args(tmp_path, monkeypatch)
    pythonw = python.with_name("pythonw.exe")
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()
    monkeypatch.setattr(colocate_service.sys, "executable", str(python))
    target = tmp_path / "colocate-service"
    monkeypatch.setattr(
        colocate_service, "_windows_service_dir", lambda: target
    )
    events = []

    class Task:
        def status(self):
            return None

        def install(self, command, *, cwd):
            events.append((command, cwd))

        def start(self):
            events.append("start")

    monkeypatch.setattr(colocate_service, "_windows_task", Task)
    colocate_service.install_colocate_service(args)
    assert events[0] == (
        [str(pythonw), str(target / "launcher.py")],
        str(target),
    )
    assert events[-1] == "start"
    launcher = (target / "launcher.py").read_text(encoding="utf-8")
    assert "colocate" in launcher
    parsed = ast.parse(launcher)
    invocations = [
        call
        for call in ast.walk(parsed)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "main"
    ]
    assert len(invocations) == 1
    argv = ast.literal_eval(invocations[0].args[0])
    assert argv == [
        "colocate",
        "--control-config",
        args.control_config,
        "--executor-config",
        args.executor_config,
    ]
    assert "WORKGATE_" in launcher
    assert "redirect_stdout" in launcher


def test_windows_executor_missing_pythonw_reports_stale_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager, _ = _paired_manager(tmp_path, system="Windows")
    monkeypatch.setattr(
        executor_service.WindowsUserTask, "available", lambda: True
    )
    monkeypatch.setattr(
        manager,
        "_windows_task",
        lambda: SimpleNamespace(
            status=lambda: {"state": "Running", "last_result": 0}
        ),
    )
    manager.service_dir.mkdir(parents=True)
    manager.launcher_path.write_text("# owned launcher")
    manager._write_service_config()
    manager._write_metadata("scheduled-task", ["pythonw.exe", "launcher.py"])
    status = manager.status()
    assert status.installed and status.running
    assert not status.runtime_current


def test_windows_colocate_requires_pythonw_before_writing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args, _python = _paired_colocate_args(tmp_path, monkeypatch)
    target = tmp_path / "not-installed"
    monkeypatch.setattr(
        colocate_service, "_windows_service_dir", lambda: target
    )

    class Task:
        def status(self):
            return None

    monkeypatch.setattr(colocate_service, "_windows_task", Task)
    with pytest.raises(RuntimeError, match="pythonw.exe"):
        colocate_service.install_colocate_service(args)
    assert not target.exists()


def test_windows_task_refuses_to_replace_unowned_task(monkeypatch):
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    calls = []

    def run(argv):
        calls.append(argv[-1])
        if "Get-ScheduledTask" in argv[-1]:
            raise RuntimeError("scheduled task belongs to another account")
        return subprocess.CompletedProcess(argv, 0, "", "")

    task = WindowsUserTask(
        "Workgate Executor",
        description="Managed by Workgate: executor",
        run=run,
    )
    with pytest.raises(RuntimeError, match="another account"):
        task.install([r"C:\pythonw.exe", r"C:\launcher.py"], cwd="C:\\")
    assert all("Register-ScheduledTask" not in script for script in calls)


def test_windows_colocate_frozen_requires_windowless_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args, _python = _paired_colocate_args(tmp_path, monkeypatch)
    target = tmp_path / "not-installed"
    monkeypatch.setattr(
        colocate_service, "_windows_service_dir", lambda: target
    )
    monkeypatch.setattr(colocate_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        colocate_service,
        "_windows_task",
        lambda: SimpleNamespace(status=lambda: None),
    )
    with pytest.raises(RuntimeError, match="windowless"):
        colocate_service.install_colocate_service(args)
    assert not target.exists()


def test_windows_task_lifecycle_and_idempotent_uninstall(monkeypatch):
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    state = {"installed": False, "running": False}
    operations = []

    def run(argv):
        script = argv[-1]
        if "Get-ScheduledTask" in script:
            data = (
                {
                    "state": "Running" if state["running"] else "Ready",
                    "last_result": 0,
                }
                if state["installed"]
                else None
            )
            return subprocess.CompletedProcess(
                argv,
                0,
                "null" if data is None else json.dumps(data),
                "",
            )
        if "Register-ScheduledTask" in script:
            state["installed"] = True
            operations.append("register")
        elif "Start-ScheduledTask" in script:
            state["running"] = True
            operations.append("start")
        elif "Stop-ScheduledTask" in script:
            state["running"] = False
            operations.append("stop")
        elif "Unregister-ScheduledTask" in script:
            state["installed"] = False
            operations.append("unregister")
        return subprocess.CompletedProcess(argv, 0, "", "")

    task = WindowsUserTask(
        "Workgate Test", description="Managed by Workgate: test", run=run
    )
    assert task.status() is None
    task.install([r"C:\pythonw.exe", r"C:\launcher.py"], cwd="C:\\")
    task.start()
    running = task.status()
    assert running is not None and running["state"] == "Running"
    task.stop()
    task.uninstall()
    task.uninstall()
    assert task.status() is None
    assert operations == ["register", "start", "stop", "unregister"]


def test_windows_task_stop_targets_only_owned_frozen_executor_image(
    monkeypatch,
):
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    scripts = []

    def run(argv):
        script = argv[-1]
        scripts.append(script)
        if "Get-ScheduledTask -TaskPath" in script:
            return subprocess.CompletedProcess(
                argv, 0, '{"state":"Running","last_result":0}', ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    task = WindowsUserTask(
        "Workgate Test", description="Managed by Workgate: test", run=run
    )
    task.stop()
    assert any("Stop-ScheduledTask" in script for script in scripts)
    cleanup = next(script for script in scripts if "Get-CimInstance" in script)
    assert "Get-ScheduledTask" in cleanup
    assert "workgatew.exe" in cleanup
    assert ".workgate" in cleanup and "runtime" in cleanup
    assert "[0-9a-f]{16}" in cleanup  # Only own updater-published PE name.
    assert "Name = '$name'" in cleanup  # Query the actual task image name.
    assert "--managed-service" in cleanup
    assert "ExecutablePath" in cleanup
    assert "taskkill.exe /T /F /PID" in cleanup


@pytest.mark.parametrize("payload", ["broken-json", "[]", '{"state": 2}'])
def test_windows_task_rejects_invalid_scheduler_status(monkeypatch, payload):
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    task = WindowsUserTask(
        "Workgate Test",
        description="Managed by Workgate: test",
        run=lambda argv: subprocess.CompletedProcess(argv, 0, payload, ""),
    )
    with pytest.raises(RuntimeError, match="invalid Windows Task Scheduler"):
        task.status()


def test_windows_task_errors_when_scheduler_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which", lambda _: None
    )
    task = WindowsUserTask(
        "Workgate Test",
        description="Managed by Workgate: test",
        run=lambda argv: subprocess.CompletedProcess(argv, 0, "null", ""),
    )
    assert not task.available()
    with pytest.raises(RuntimeError, match="requires PowerShell"):
        task.status()
    with pytest.raises(ValueError, match="must not be empty"):
        task.install([], cwd="C:\\")


def test_windows_task_cancels_only_its_upgrade_timer(monkeypatch):
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    scripts = []

    def run(argv):
        scripts.append(argv[-1])
        return subprocess.CompletedProcess(
            argv,
            0,
            '{"state":"Running","last_result":0}'
            if "Get-ScheduledTaskInfo" in argv[-1]
            else "",
            "",
        )

    task = WindowsUserTask(
        "Test Workgate", description="Managed by Workgate: test", run=run
    )
    task.cancel_runtime_restart()
    assert len(scripts) == 2
    assert "-Trigger @($logon[0])" in scripts[1]
    assert "unexpected scheduled task triggers" in scripts[1]
    assert "PT1M" in scripts[1] and "PT5M" in scripts[1]
    assert "Unregister-ScheduledTask" not in scripts[1]


def test_windows_task_status_exposes_owned_native_action(monkeypatch):
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    scripts = []
    native = {
        "state": "Running",
        "last_result": 0,
        "action": {
            "execute": r"C:\Tools\Python\pythonw.exe",
            "arguments": r'"C:\Data dir\launcher.py"',
            "working_directory": r"C:\Data dir",
        },
    }

    def run(argv):
        scripts.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, json.dumps(native), "")

    task = WindowsUserTask(
        "Workgate Executor",
        description="Managed by Workgate: executor",
        run=run,
    )
    assert task.status() == native
    assert "@($task.Actions).Count -eq 1" in scripts[-1]
    assert "$native.Execute" in scripts[-1]
    assert "$native.Arguments" in scripts[-1]
    assert "$native.WorkingDirectory" in scripts[-1]
    assert "scheduled task belongs to another account" in scripts[-1]


@pytest.mark.parametrize("field", ["execute", "arguments", "working_directory"])
def test_windows_executor_native_action_mismatch_marks_runtime_stale(
    tmp_path, monkeypatch, field
):
    manager, _ = _paired_manager(tmp_path, system="Windows")
    pythonw = manager.executable.with_name("pythonw.exe")
    pythonw.parent.mkdir(parents=True, exist_ok=True)
    pythonw.touch()
    command = manager._runtime_command()
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("scheduled-task", command)
    native = {
        "state": "Running",
        "last_result": 0,
        "action": {
            "execute": command[0],
            "arguments": subprocess.list2cmdline(command[1:]),
            "working_directory": str(manager.service_dir.resolve()),
        },
    }
    monkeypatch.setattr(
        manager, "_windows_task", lambda: SimpleNamespace(status=lambda: native)
    )
    assert manager._runtime_current()
    assert manager.status().runtime_current is True
    native["action"][field] += "-tampered"
    after = manager.status()
    assert after.state.value == "running"
    assert after.runtime_current is False


def test_windows_executor_missing_or_multiple_native_actions_mark_runtime_stale(
    tmp_path, monkeypatch
):
    manager, _ = _paired_manager(tmp_path, system="Windows")
    pythonw = manager.executable.with_name("pythonw.exe")
    pythonw.parent.mkdir(parents=True, exist_ok=True)
    pythonw.touch()
    manager._write_service_config()
    manager._write_launcher()
    manager._write_metadata("scheduled-task", manager._runtime_command())
    assert manager._runtime_current()

    class Task:
        def __init__(self, response):
            self.response = response

        def status(self):
            return self.response

    for bad in (None, {}, {"execute": ""}):
        native = {"state": "Running", "last_result": 0, "action": bad}
        monkeypatch.setattr(
            manager, "_windows_task", lambda value=native: Task(value)
        )
        assert manager.status().runtime_current is False
