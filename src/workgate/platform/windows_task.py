"""Per-user Windows Task Scheduler operations shared by Executor and Colocate."""

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def is_windowless_pe(path: Path) -> bool:
    """Check that a frozen Windows Task Scheduler target is actually GUI-subsystem."""
    try:
        with path.open("rb") as executable:
            header = executable.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                return False
            offset = int.from_bytes(header[0x3C:0x40], "little")
            if offset > 65536:
                return False
            executable.seek(offset)
            if executable.read(4) != b"PE\0\0":
                return False
            executable.seek(offset + 4 + 20 + 68)
            return int.from_bytes(executable.read(2), "little") == 2
    except OSError:
        return False


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class WindowsUserTask:
    """Manage one explicitly owned, least-privilege logon task."""

    def __init__(self, name: str, *, description: str, run: RunCommand) -> None:
        self.name = name
        self.description = description
        self._run = run

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("powershell.exe") or shutil.which("pwsh.exe"))

    def _powershell(self, script: str) -> subprocess.CompletedProcess[str]:
        executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not executable:
            raise RuntimeError("Windows Task Scheduler requires PowerShell")
        return self._run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference = 'Stop'; " + script,
            ]
        )

    @property
    def _task(self) -> str:
        return f"-TaskPath '\\' -TaskName {_literal(self.name)}"

    def status(self) -> dict[str, Any] | None:
        """Verify ownership before returning Task Scheduler state."""
        script = "\n".join(
            [
                "$tasks = @(Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop | "
                f"Where-Object {{ $_.TaskName -eq {_literal(self.name)} }})",
                "if ($tasks.Count -eq 0) { [Console]::Out.Write('null'); exit 0 }",
                "if ($tasks.Count -ne 1) { throw 'duplicate task name' }",
                "$task = $tasks[0]",
                "$identity = [Security.Principal.WindowsIdentity]::GetCurrent()",
                f"if ($task.Description -cne {_literal(self.description)}) {{ "
                "throw 'refusing to manage an unmanaged scheduled task' }",
                "$userId = [string]$task.Principal.UserId",
                "$taskSid = if ($userId -like 'S-1-*') { $userId } else { "
                "([Security.Principal.NTAccount]::new($userId)).Translate("
                "[Security.Principal.SecurityIdentifier]).Value }",
                "if ($taskSid -ne $identity.User.Value) { "
                "throw 'scheduled task belongs to another account' }",
                "if ($task.Principal.RunLevel.ToString() -ne 'Limited' -or "
                "$task.Principal.LogonType.ToString() -ne 'Interactive') { "
                "throw 'scheduled task has unsafe privileges or login mode' }",
                "$info = Get-ScheduledTaskInfo -InputObject $task",
                "[Console]::Out.Write((@{state=$task.State.ToString(); "
                "last_result=[int64]$info.LastTaskResult} | ConvertTo-Json -Compress))",
            ]
        )
        result = self._powershell(script)
        try:
            data = json.loads(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(
                "invalid Windows Task Scheduler response"
            ) from exc
        if data is None:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            raise RuntimeError("invalid Windows Task Scheduler state")
        return data

    def install(self, command: list[str], *, cwd: str) -> None:
        """Register an interactive, non-elevated task for the current login."""
        if not command:
            raise ValueError("scheduled task command must not be empty")
        self.stop()
        arguments = subprocess.list2cmdline(command[1:])
        script = "\n".join(
            [
                "$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name",
                "$action = New-ScheduledTaskAction "
                f"-Execute {_literal(command[0])} "
                f"-Argument {_literal(arguments)} "
                f"-WorkingDirectory {_literal(cwd)}",
                "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
                "$settings = New-ScheduledTaskSettingsSet "
                "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                "-ExecutionTimeLimit ([TimeSpan]::Zero) "
                "-MultipleInstances IgnoreNew -RestartCount 3 "
                "-RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable",
                "$principal = New-ScheduledTaskPrincipal -UserId $user "
                "-LogonType Interactive -RunLevel Limited",
                "Register-ScheduledTask "
                f"{self._task} -Action $action -Trigger $trigger "
                "-Settings $settings -Principal $principal "
                f"-Description {_literal(self.description)} "
                "-Force | Out-Null",
            ]
        )
        self._powershell(script)

    def start(self) -> None:
        if self.status() is None:
            raise RuntimeError(f"scheduled task is not installed: {self.name}")
        self._powershell(f"Start-ScheduledTask {self._task}")

    def switch_runtime(self, *, expected: str, replacement: str) -> None:
        """Switch one owned task's executable without stopping its running instance."""
        if self.status() is None:
            raise RuntimeError(f"scheduled task is not installed: {self.name}")
        script = "\n".join(
            [
                f"$task = Get-ScheduledTask {self._task} -ErrorAction Stop",
                "if (@($task.Actions).Count -ne 1) { "
                "throw 'unexpected scheduled task action count' }",
                "$action = $task.Actions[0]",
                f"$expected = [IO.Path]::GetFullPath({_literal(expected)})",
                "$found = [IO.Path]::GetFullPath([string]$action.Execute)",
                "if (-not [string]::Equals($expected, $found, "
                "[StringComparison]::OrdinalIgnoreCase)) { "
                "throw 'scheduled task executable changed unexpectedly' }",
                "$logon = @($task.Triggers | Where-Object { "
                "$_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })",
                "if ($logon.Count -ne 1) { "
                "throw 'expected a single logon trigger' }",
                "$newAction = New-ScheduledTaskAction "
                f"-Execute {_literal(replacement)} "
                "-Argument $action.Arguments "
                "-WorkingDirectory $action.WorkingDirectory",
                "$oneTime = New-ScheduledTaskTrigger -Once "
                "-At ((Get-Date).AddSeconds(45)) "
                "-RepetitionInterval (New-TimeSpan -Minutes 1) "
                "-RepetitionDuration (New-TimeSpan -Minutes 5)",
                "$triggers = @($logon[0], $oneTime)",
                f"Set-ScheduledTask {self._task} "
                "-Action $newAction -Trigger $triggers -ErrorAction Stop "
                "| Out-Null",
            ]
        )
        self._powershell(script)

    def cancel_runtime_restart(self) -> None:
        """Remove only our pending update trigger before an explicit stop/restart."""
        if self.status() is None:
            raise RuntimeError(f"scheduled task is not installed: {self.name}")
        self._powershell(
            "\n".join(
                [
                    f"$task = Get-ScheduledTask {self._task} -ErrorAction Stop",
                    "$all = @($task.Triggers)",
                    "$logon = @($all | Where-Object { "
                    "$_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })",
                    "$timer = @($all | Where-Object { "
                    "$_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' })",
                    "if ($timer.Count -eq 0) { return }",
                    "if ($all.Count -ne 2 -or $logon.Count -ne 1 -or "
                    "$timer.Count -ne 1 -or "
                    "$timer[0].Repetition.Interval -ne 'PT1M' -or "
                    "$timer[0].Repetition.Duration -ne 'PT5M') { "
                    "throw 'unexpected scheduled task triggers' }",
                    f"Set-ScheduledTask {self._task} "
                    "-Trigger @($logon[0]) -ErrorAction Stop | Out-Null",
                ]
            )
        )

    def stop(self) -> None:
        current = self.status()
        if current is None:
            return
        if current["state"].lower() in {"running", "queued"}:
            self._powershell(f"Stop-ScheduledTask {self._task}")
        # One-file PyInstaller starts a second process: Task Scheduler can stop
        # its bootloader while the child still owns executor.log. Only terminate
        # processes using this verified task's *exact* windowless service image.
        script = "\n".join(
            [
                f"$task = Get-ScheduledTask {self._task} -ErrorAction Stop",
                "if (@($task.Actions).Count -ne 1) { throw 'unexpected task actions' }",
                "$action = $task.Actions[0]",
                "$exe = [IO.Path]::GetFullPath([string]$action.Execute)",
                "if ([IO.Path]::GetFileName($exe) -ieq 'workgatew.exe' -and "
                "[string]$action.Arguments -match '--managed-service') {",
                "  Get-CimInstance Win32_Process -Filter \"Name = 'workgatew.exe'\" | "
                "Where-Object { $_.ExecutablePath -and $_.CommandLine -and "
                "[string]::Equals([string]$_.ExecutablePath, $exe, "
                "[StringComparison]::OrdinalIgnoreCase) -and "
                "$_.CommandLine.Contains([string]$action.Arguments) } | ForEach-Object {",
                "    & taskkill.exe /T /F /PID $_.ProcessId | Out-Null",
                "  }",
                "}",
            ]
        )
        self._powershell(script)

    def uninstall(self) -> None:
        if self.status() is None:
            return
        self.stop()
        self._powershell(
            f"Unregister-ScheduledTask {self._task} -Confirm:$false"
        )
        if self.status() is not None:
            raise RuntimeError(f"scheduled task was not removed: {self.name}")
