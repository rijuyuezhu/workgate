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
                "$action = $null",
                "if (@($task.Actions).Count -eq 1) {",
                "  $native = $task.Actions[0]",
                "  $action = @{execute=[string]$native.Execute; "
                "arguments=[string]$native.Arguments; "
                "working_directory=[string]$native.WorkingDirectory}",
                "}",
                "[Console]::Out.Write((@{state=$task.State.ToString(); "
                "last_result=[int64]$info.LastTaskResult; action=$action} "
                "| ConvertTo-Json -Compress))",
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

    def install(
        self,
        command: list[str],
        *,
        cwd: str,
        private_state_dir: str | None = None,
    ) -> None:
        """Register an interactive, non-elevated task for the current login."""
        if not command:
            raise ValueError("scheduled task command must not be empty")
        self.stop()
        if private_state_dir is not None:
            # Elevated installation may create Administrator-owned files. The
            # scheduled task runs with a filtered token for this same user.
            self._powershell(
                "\n".join(
                    [
                        "$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
                        f"$dir = {_literal(private_state_dir)}",
                        "if (-not (Test-Path -LiteralPath $dir -PathType Container)) { "
                        "throw 'private service state directory is missing' }",
                        "& icacls.exe $dir /grant:r ('*' + $sid + ':(OI)(CI)F') /T "
                        "| Out-Null",
                        "if ($LASTEXITCODE -ne 0) { "
                        "throw 'cannot grant the limited task owner access to service state' }",
                    ]
                )
            )
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
                # A task registered by an elevated installer can grant the
                # ordinary user only read access. The task's Limited token
                # must still be able to update it on the next source cutover.
                "$scheduler = New-Object -ComObject Schedule.Service",
                "$scheduler.Connect()",
                f"$registered = $scheduler.GetFolder('\\').GetTask({_literal(self.name)})",
                "$acl = New-Object "
                "System.Security.AccessControl.RawSecurityDescriptor("
                "$registered.GetSecurityDescriptor(4))",
                "$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User",
                "$ownerFull = @($acl.DiscretionaryAcl | Where-Object { "
                "$_.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed "
                "-and $_.SecurityIdentifier.Value -eq $sid.Value "
                "-and ($_.AccessMask -band 0x1F01FF) -eq 0x1F01FF })",
                "if ($ownerFull.Count -eq 0) {",
                # Task Scheduler's default DACL is not always canonical.
                "$ace = [Security.AccessControl.CommonAce]::new("
                "[Security.AccessControl.AceFlags]::None, "
                "[Security.AccessControl.AceQualifier]::AccessAllowed, "
                "0x1F01FF, $sid, $false, $null)",
                "$acl.DiscretionaryAcl.InsertAce($acl.DiscretionaryAcl.Count, $ace)",
                "$registered.SetSecurityDescriptor("
                "$acl.GetSddlForm([Security.AccessControl.AccessControlSections]::Access), 0)",
                "}",
                "$updated = New-Object "
                "System.Security.AccessControl.RawSecurityDescriptor("
                "$registered.GetSecurityDescriptor(4))",
                "$ownerFull = @($updated.DiscretionaryAcl | Where-Object { "
                "$_.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed "
                "-and $_.SecurityIdentifier.Value -eq $sid.Value "
                "-and ($_.AccessMask -band 0x1F01FF) -eq 0x1F01FF })",
                "if ($ownerFull.Count -eq 0) { "
                "throw 'scheduled task owner lacks update access' }",
            ]
        )
        self._powershell(script)

    def schedule_once(self, command: list[str], *, cwd: str) -> None:
        """Schedule a bounded user task, reusing only a verified completed slot.

        Task Scheduler owns its process tree independently from a managed
        Executor task. An expiration boundary cleans up even after a crash.
        """
        if not command:
            raise ValueError("scheduled task command must not be empty")
        arguments = subprocess.list2cmdline(command[1:])
        script = "\n".join(
            [
                "$identity = [Security.Principal.WindowsIdentity]::GetCurrent()",
                "$user = $identity.Name",
                f"$exe = {_literal(command[0])}",
                f"$args = {_literal(arguments)}",
                f"$cwd = {_literal(cwd)}",
                f"$existing = Get-ScheduledTask {self._task} -ErrorAction SilentlyContinue",
                "if ($null -ne $existing) {",
                f"  if ($existing.Description -cne {_literal(self.description)}) {{ "
                "throw 'refusing to replace an unmanaged task' }",
                "  $userId = [string]$existing.Principal.UserId",
                "  $taskSid = if ($userId -like 'S-1-*') { $userId } else { "
                "([Security.Principal.NTAccount]::new($userId)).Translate("
                "[Security.Principal.SecurityIdentifier]).Value }",
                "  if ($taskSid -ne $identity.User.Value -or "
                "$existing.Principal.RunLevel.ToString() -ne 'Limited' -or "
                "$existing.Principal.LogonType.ToString() -ne 'Interactive') { "
                "throw 'refusing to replace a task with different ownership' }",
                "  $info = Get-ScheduledTaskInfo -InputObject $existing",
                "  if ($existing.State.ToString() -ne 'Ready' -or "
                "$info.LastTaskResult -ne 0 -or $info.LastRunTime.Year -lt 2000) { "
                "throw 'source cutover task is already registered' }",
                "  if (@($existing.Actions).Count -ne 1 -or "
                "@($existing.Triggers).Count -ne 1 -or "
                "$existing.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskTimeTrigger') { "
                "throw 'refusing to replace an unexpected cutover task' }",
                "  $previous = $existing.Actions[0]",
                "  if ([string]$previous.Execute -ine $exe -or "
                "[string]$previous.Arguments -cne $args -or "
                "[string]$previous.WorkingDirectory -ine $cwd) { "
                "throw 'refusing to replace a different cutover' }",
                f"  Unregister-ScheduledTask {self._task} -Confirm:$false -ErrorAction Stop",
                "}",
                f"if (Get-ScheduledTask {self._task} -ErrorAction SilentlyContinue) {{ "
                "throw 'refusing to replace a scheduled task' }",
                "$action = New-ScheduledTaskAction "
                "-Execute $exe -Argument $args -WorkingDirectory $cwd",
                "$trigger = New-ScheduledTaskTrigger -Once "
                "-At ((Get-Date).AddSeconds(15))",
                "$trigger.EndBoundary = (Get-Date).AddMinutes(30).ToString('s')",
                "$settings = New-ScheduledTaskSettingsSet "
                "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                "-ExecutionTimeLimit (New-TimeSpan -Minutes 30) "
                "-DeleteExpiredTaskAfter (New-TimeSpan -Minutes 5) "
                "-MultipleInstances IgnoreNew -StartWhenAvailable",
                "$principal = New-ScheduledTaskPrincipal -UserId $user "
                "-LogonType Interactive -RunLevel Limited",
                "Register-ScheduledTask "
                f"{self._task} -Action $action -Trigger $trigger "
                "-Settings $settings -Principal $principal "
                f"-Description {_literal(self.description)} "
                "-ErrorAction Stop | Out-Null",
            ]
        )
        self._powershell(script)

    def start(self) -> None:
        if self.status() is None:
            raise RuntimeError(f"scheduled task is not installed: {self.name}")
        self._powershell(f"Start-ScheduledTask {self._task}")

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
                "$name = [IO.Path]::GetFileName($exe)",
                # Updates use a uniquely named PE rather than overwriting a
                # mapped image. Reject all other image names even for owned tasks.
                "if (($name -ieq 'workgatew.exe' -or "
                r"$name -match '^\.workgate\.runtime\.[0-9]+\.[0-9a-f]{16}\.exe$') -and "
                "[string]$action.Arguments -match '--managed-service') {",
                "  Get-CimInstance Win32_Process -Filter (\"Name = '$name'\") | "
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
