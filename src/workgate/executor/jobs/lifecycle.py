"""Shell-attempt materialization and durable job-status reconciliation."""

import re
import shlex
import subprocess
import sys
from pathlib import Path

from ...jobs import status as job_status
from ...jobs.persistence import attempt_paths as _attempt_paths
from ...jobs.state import JobAttemptPaths, MutableJobRow
from ...utils.private_files import write_private_text
from ..config import ExecutorConfig
from ..shell import check_command_policy

_adopt_pending_retry = job_status._adopt_pending_retry
_clear_pending_retry = job_status._clear_pending_retry
_read_status = job_status._read_status
_read_status_path = job_status._read_status_path
_observed_active_operation = job_status._observed_active_operation


def _refresh_job_status(
    job: MutableJobRow,
    active_shells: set[str] | None,
    now: float | None = None,
    *,
    managed_job_has_local_task: job_status.ManagedJobStateProbe,
    managed_job_liveness: job_status.ManagedJobLivenessProbe,
    read_status: job_status.JobStatusReader = _read_status,
    read_status_path: job_status.JobStatusPathReader = _read_status_path,
) -> MutableJobRow:
    """Compatibility seam over process-neutral status reconciliation."""
    return job_status._refresh_job_status(
        job,
        active_shells,
        now,
        managed_job_has_local_task=managed_job_has_local_task,
        managed_job_liveness=managed_job_liveness,
        read_status=read_status,
        read_status_path=read_status_path,
        observed_active_operation=_observed_active_operation,
    )


def _shell_safe_name(value: str) -> str:
    """Return a stable persistent-shell name derived from a job name."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", value.strip())[:48].strip(".-")
    return cleaned or "job"


def _runner_argv(
    config: ExecutorConfig, paths: JobAttemptPaths, cwd: Path
) -> list[str]:
    """Build the internal durable runner invocation for one executor attempt."""
    arguments = [
        "--command-file",
        str(paths["command"]),
        "--log-file",
        str(paths["log"]),
        "--status-file",
        str(paths["status"]),
        "--cwd",
        str(cwd),
        "--shell",
        config.shell_executable,
        "--max-log-bytes",
        str(max(1, config.max_job_log_bytes)),
    ]
    if getattr(sys, "frozen", False):
        return [sys.executable, "job-runner", *arguments]
    return [
        sys.executable,
        str(Path(__file__).resolve().with_name("runner_bootstrap.py")),
        *arguments,
    ]


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _runner_command(argv: list[str], shell: str) -> str:
    """Quote an internal runner invocation for the configured parent shell."""
    name = Path(shell).name.lower()
    if name in {"powershell.exe", "powershell", "pwsh.exe", "pwsh"}:
        return "& " + " ".join(_powershell_quote(value) for value in argv)
    if name in {"cmd.exe", "cmd"}:
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _prepare_attempt(
    config: ExecutorConfig,
    job_id: str,
    attempt: int,
    command: str,
    cwd: Path,
) -> tuple[JobAttemptPaths, str]:
    """Validate and materialize one executor-owned durable shell attempt."""
    check_command_policy(config, command)
    paths = _attempt_paths(job_id, attempt)
    write_private_text(paths["command"], command)
    paths["log"].unlink(missing_ok=True)
    paths["status"].unlink(missing_ok=True)
    argv = _runner_argv(config, paths, cwd)
    return paths, _runner_command(argv, config.shell_executable)
