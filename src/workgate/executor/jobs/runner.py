"""Internal durable shell-attempt runner."""

import argparse
import contextlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ...errors import public_error_type
from ...jobs.logs import compact_log
from ...platform.shell import shell_argv
from ...utils.private_files import atomic_write_private_text
from ...utils.processes import user_subprocess_env
from ..bounded_runner import bounded_runner_argv


def _utc() -> float:
    """Return the current Unix timestamp."""
    return time.time()


def _runner_shell_args(shell: str, command: str) -> list[str]:
    """Build direct subprocess arguments for the configured user shell."""
    return shell_argv(shell, command, login=True)


def configure_job_runner_parser(
    parser: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    """Register the private durable-runner arguments on one parser."""
    parser.add_argument("--command-file", required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--shell", required=True)
    parser.add_argument("--max-log-bytes", type=int, required=True)
    parser.add_argument("--blocked-env-name", action="append", default=[])
    parser.add_argument("--blocked-env-prefix", action="append", default=[])
    return parser


def _close_job_when_root_exits(
    process: subprocess.Popen[bytes], job: Any
) -> None:
    """Reap a completed root and release its surviving descendant handles."""
    try:
        process.wait()
    finally:
        job.close()


def run_job_runner_from_args(args: Any) -> None:
    """Run one internal durable job attempt and write its terminal status."""
    command_path = Path(args.command_file)
    log_path = Path(args.log_file)
    status_path = Path(args.status_file)
    cwd = Path(args.cwd)
    shell = str(args.shell)
    max_log_bytes = max(1, int(args.max_log_bytes))
    completed_at = _utc()
    exit_code: int | None = None
    error: str | None = None
    output_bytes = 0
    log_truncated = False
    process: subprocess.Popen[bytes] | None = None
    job: Any | None = None
    watcher: threading.Thread | None = None

    try:
        command = command_path.read_text(encoding="utf-8")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w+b") as log:
            with contextlib.suppress(OSError):
                log_path.chmod(0o600)
            native_windows = os.name == "nt"
            if native_windows:
                from ...platform.windows_job import WindowsJob

                job = WindowsJob()
            process = subprocess.Popen(
                (
                    bounded_runner_argv(shell, command)
                    if native_windows
                    else _runner_shell_args(shell, command)
                ),
                cwd=cwd,
                stdin=subprocess.PIPE if native_windows else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=0x08000000 if native_windows else 0,
                env=user_subprocess_env(
                    blocked_names=tuple(
                        getattr(args, "blocked_env_name", ()) or ()
                    ),
                    blocked_prefixes=tuple(
                        getattr(args, "blocked_env_prefix", ()) or ()
                    ),
                ),
            )
            if job is not None:
                job.attach_waiting_process(process.pid)
                assert process.stdin is not None
                process.stdin.write(b"\x01")
                process.stdin.flush()
                process.stdin.close()
                # A background descendant may retain stdout after the root
                # exits. Close the job concurrently so reading reaches EOF.
                watcher = threading.Thread(
                    target=_close_job_when_root_exits,
                    args=(process, job),
                    daemon=True,
                )
                watcher.start()
            if process.stdout is None:
                raise RuntimeError(
                    "job runner failed to capture process output"
                )
            read_available = getattr(
                process.stdout, "read1", process.stdout.read
            )
            while True:
                chunk = read_available(65536)
                if not chunk:
                    break
                output_bytes += len(chunk)
                log.write(chunk)
                log.flush()
                if log.tell() > max_log_bytes * 2:
                    log_truncated = (
                        compact_log(log, max_log_bytes) or log_truncated
                    )
            exit_code = process.wait()
            log_truncated = compact_log(log, max_log_bytes) or log_truncated
    except BaseException as exc:
        if job is not None:
            job.close()
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()
                process.wait(timeout=2)
        error = f"{public_error_type(exc)}: {exc}"
    finally:
        if job is not None:
            job.close()
        if watcher is not None:
            watcher.join(timeout=2)
        completed_at = _utc()
        atomic_write_private_text(
            status_path,
            json.dumps(
                {
                    "exit_code": exit_code,
                    "completed_at": completed_at,
                    "error": error,
                    "log_truncated": log_truncated,
                    "output_bytes": output_bytes,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    if error is not None:
        raise SystemExit(1)
    raise SystemExit(exit_code or 0)


def main(argv: list[str] | None = None) -> None:
    """Run one durable job attempt without importing the control CLI."""
    parser = configure_job_runner_parser(
        argparse.ArgumentParser(
            description="Run one durable tracked-job attempt. Internal interface."
        )
    )
    run_job_runner_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    main()
