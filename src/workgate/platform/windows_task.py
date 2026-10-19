"""Per-user Windows Task Scheduler operations shared by Executor and Colocate."""

import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


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

    def stop(self) -> None:
        current = self.status()
        if current is not None and current["state"].lower() in {
            "running",
            "queued",
        }:
            self._powershell(f"Stop-ScheduledTask {self._task}")

    def uninstall(self) -> None:
        if self.status() is None:
            return
        self.stop()
        self._powershell(
            f"Unregister-ScheduledTask {self._task} -Confirm:$false"
        )
        if self.status() is not None:
            raise RuntimeError(f"scheduled task was not removed: {self.name}")
