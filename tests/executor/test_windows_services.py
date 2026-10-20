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
from workgate.executor.runtime_update import ExecutorRuntimeStateStore
from workgate.persistence import FileStateStore
from workgate.platform.windows_task import WindowsUserTask, _literal
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.executor import ExecutorRuntimeOwnership
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


def _fake_gui_pe(*, subsystem: int = 2) -> bytes:
    executable = bytearray(256)
    executable[:2] = b"MZ"
    executable[0x3C:0x40] = (128).to_bytes(4, "little")
    executable[128:132] = b"PE\0\0"
    executable[128 + 4 + 20 + 68 : 128 + 4 + 20 + 70] = subsystem.to_bytes(
        2, "little"
    )
    return bytes(executable)


def test_windows_frozen_service_rejects_console_subsystem_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile = _paired_manager(tmp_path, system="Windows")
    manager.executable = tmp_path / "workgate.exe"
    (tmp_path / "workgatew.exe").write_bytes(_fake_gui_pe(subsystem=3))
    monkeypatch.setattr(executor_service.sys, "frozen", True, raising=False)
    with pytest.raises(
        executor_service.UnsupportedExecutorServiceError,
        match="valid windowless",
    ):
        manager.install()
    assert not manager.config_path.exists()
    assert not manager.metadata_path.exists()


def test_windows_frozen_service_requires_windowless_sibling_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager, _profile = _paired_manager(tmp_path, system="Windows")
    manager.executable = tmp_path / "workgate.exe"
    monkeypatch.setattr(executor_service.sys, "frozen", True, raising=False)
    with pytest.raises(
        executor_service.UnsupportedExecutorServiceError, match="workgatew.exe"
    ):
        manager.install()
    assert not manager.config_path.exists()
    assert not manager.metadata_path.exists()


def test_windows_frozen_service_uses_windowless_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager, _profile = _paired_manager(tmp_path, system="Windows")
    manager.executable = tmp_path / "workgate.exe"
    sibling = tmp_path / "workgatew.exe"
    sibling.write_bytes(_fake_gui_pe())
    monkeypatch.setattr(executor_service.sys, "frozen", True, raising=False)
    command = manager._runtime_command()
    assert command[:4] == [str(sibling), "executor", "run", "--managed-service"]
    assert command[4:6] == ["--runtime-ownership", "self-contained"]


def test_windows_frozen_sibling_can_install_paired_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _profile = _paired_manager(tmp_path, system="Windows")
    manager.executable = tmp_path / "workgate.exe"
    windowless = tmp_path / "workgatew.exe"
    windowless.write_bytes(_fake_gui_pe())
    monkeypatch.setattr(executor_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        executor_service.WindowsUserTask, "available", lambda: True
    )
    installed = False
    started = False
    recorded = []

    class Task:
        def status(self):
            return (
                {"state": "Running" if started else "Ready", "last_result": 0}
                if installed
                else None
            )

        def install(self, command, *, cwd):
            nonlocal installed
            installed = True
            recorded.append(command)

        def start(self):
            nonlocal started
            started = True

    monkeypatch.setattr(manager, "_windows_task", Task)
    result = manager.install()
    assert result.installed and result.running
    assert recorded[0][0] == str(windowless)
    assert "--managed-service" in recorded[0]
    assert (
        not manager.launcher_path.exists()
    )  # Frozen service needs no Python shim.
    metadata = manager._read_metadata()
    assert metadata is not None and metadata["command"] == recorded[0]
    assert manager._runtime_command(ExecutorRuntimeOwnership.CONTROL_MANAGED)[
        0
    ] == str(windowless)


def test_windows_control_managed_install_records_service_not_cli_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    manager, _ = _paired_manager(tmp_path, system="Windows")
    manager.executable = tmp_path / "workgate.exe"
    windowless = tmp_path / "workgatew.exe"
    windowless.write_bytes(_fake_gui_pe())
    monkeypatch.setattr(executor_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        executor_service.sys, "executable", str(manager.executable)
    )
    monkeypatch.setattr(
        executor_service.WindowsUserTask, "available", lambda: True
    )

    class Task:
        def install(self, command, *, cwd):
            assert command[0] == str(windowless)

        def start(self):
            pass

        def status(self):
            return {"state": "Running", "last_result": 0}

    monkeypatch.setattr(manager, "_windows_task", Task)
    manager.install(runtime_ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)
    state = ExecutorRuntimeStateStore(manager.state_store).load()
    assert state is not None
    assert state.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
    assert state.executable == str(windowless)


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


def test_windows_task_updates_owned_executable_without_stopping_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    scripts: list[str] = []

    def run(argv):
        script = argv[-1]
        scripts.append(script)
        return subprocess.CompletedProcess(
            argv,
            0,
            '{"state":"Running","last_result":0}'
            if "Get-ScheduledTaskInfo" in script
            else "",
            "",
        )

    task = WindowsUserTask(
        "Test Workgate", description="Managed by Workgate: test", run=run
    )
    task.switch_runtime(
        expected=r"C:\Program Files\Old Workgate\workgate.exe",
        replacement=r"C:\Users\O'Brien\New Workgate\workgate.exe",
    )
    assert len(scripts) == 2
    update = scripts[-1]
    assert "Get-ScheduledTask -TaskPath" in update
    assert "Set-ScheduledTask -TaskPath" in update
    assert "expected a single logon trigger" in update
    assert "executable changed unexpectedly" in update
    assert "O''Brien" in update
    assert "AddSeconds(45)" in update
    assert "RepetitionDuration" in update
    assert "Stop-ScheduledTask" not in update
    assert "Unregister-ScheduledTask" not in update


def test_windows_task_refuses_unowned_runtime_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workgate.platform.windows_task.shutil.which",
        lambda _: "powershell.exe",
    )
    scripts = []

    def run(argv):
        scripts.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "null", "")

    task = WindowsUserTask(
        "Missing Workgate", description="Managed by Workgate: test", run=run
    )
    with pytest.raises(RuntimeError, match="not installed"):
        task.switch_runtime(expected=r"C:\old.exe", replacement=r"C:\new.exe")
    assert len(scripts) == 1


def test_windows_runtime_switch_preserves_existing_task_and_cli_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _paired_manager(tmp_path, system="Windows")
    old = tmp_path / "original.exe"
    new = tmp_path / "verified version.exe"
    new.write_bytes(_fake_gui_pe())
    command = [
        str(old),
        "executor",
        "run",
        "--managed-service",
        "--config",
        str(tmp_path / "config.json"),
    ]
    manager._write_metadata("scheduled-task", command)
    calls = []

    class Task:
        def switch_runtime(self, *, expected, replacement):
            calls.append((expected, replacement))

    monkeypatch.setattr(manager, "_windows_task", Task)
    manager.activate_windows_runtime(old, new)
    assert calls == [(str(old), str(new))]
    metadata = manager._read_metadata()
    assert metadata is not None
    assert metadata["command"] == [str(new), *command[1:]]


def test_windows_runtime_switch_recovers_task_on_metadata_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _paired_manager(tmp_path, system="Windows")
    old = tmp_path / "original.exe"
    new = tmp_path / "verified.exe"
    new.write_bytes(_fake_gui_pe())
    manager._write_metadata("scheduled-task", [str(old), "executor", "run"])
    calls = []

    class Task:
        def switch_runtime(self, *, expected, replacement):
            calls.append((expected, replacement))

    monkeypatch.setattr(manager, "_windows_task", Task)
    monkeypatch.setattr(
        manager,
        "_write_metadata",
        lambda *_args: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(OSError, match="denied"):
        manager.activate_windows_runtime(old, new)
    assert calls == []  # A metadata write failure must not touch the task.
    metadata = manager._read_metadata()
    assert metadata is not None
    assert metadata["command"][0] == str(old)


def test_windows_runtime_switch_failure_restores_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _paired_manager(tmp_path, system="Windows")
    old = tmp_path / "old.exe"
    new = tmp_path / "new.exe"
    new.write_bytes(_fake_gui_pe())
    manager._write_metadata("scheduled-task", [str(old), "executor", "run"])

    class Task:
        def switch_runtime(self, *, expected, replacement):
            assert (expected, replacement) == (str(old), str(new))
            raise RuntimeError("Task Scheduler refused switch")

    monkeypatch.setattr(manager, "_windows_task", Task)
    with pytest.raises(RuntimeError, match="refused switch"):
        manager.activate_windows_runtime(old, new)
    metadata = manager._read_metadata()
    assert metadata is not None
    assert metadata["command"] == [str(old), "executor", "run"]


def test_windows_runtime_switch_reports_failed_metadata_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _paired_manager(tmp_path, system="Windows")
    old, new = tmp_path / "old.exe", tmp_path / "new.exe"
    new.write_bytes(_fake_gui_pe())
    manager._write_metadata("scheduled-task", [str(old), "executor", "run"])
    original_write = manager._write_metadata
    writes = []

    def record_write(backend, command):
        writes.append(command)
        if len(writes) == 2:
            raise OSError("metadata rollback refused")
        original_write(backend, command)

    class Task:
        def switch_runtime(self, *, expected, replacement):
            raise RuntimeError("native switch failed")

    monkeypatch.setattr(manager, "_write_metadata", record_write)
    monkeypatch.setattr(manager, "_windows_task", Task)
    with pytest.raises(RuntimeError, match="metadata rollback failed"):
        manager.activate_windows_runtime(old, new)
    assert writes == [
        [str(new), "executor", "run"],
        [str(old), "executor", "run"],
    ]


def test_windows_runtime_switch_rejects_console_executable_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _paired_manager(tmp_path, system="Windows")
    old, new = tmp_path / "old.exe", tmp_path / "new.exe"
    new.write_bytes(_fake_gui_pe(subsystem=3))
    manager._write_metadata("scheduled-task", [str(old), "executor", "run"])
    monkeypatch.setattr(
        manager, "_windows_task", lambda: pytest.fail("task must not change")
    )
    with pytest.raises(RuntimeError, match="not a windowless executable"):
        manager.activate_windows_runtime(old, new)
    metadata = manager._read_metadata()
    assert metadata is not None and metadata["command"][0] == str(old)


def test_windows_runtime_switch_rejects_wrong_owner_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _paired_manager(tmp_path, system="Windows")
    monkeypatch.setattr(
        manager, "_windows_task", lambda: pytest.fail("must not update task")
    )
    with pytest.raises(RuntimeError, match="does not match"):
        manager.activate_windows_runtime(
            tmp_path / "old.exe", tmp_path / "new.exe"
        )


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
