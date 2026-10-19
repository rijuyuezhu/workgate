"""Launch ordinary Control and Executor CLI processes on one machine."""

import argparse
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from typing import Any


def register_colocate_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the same-host launcher and optional systemd installation."""
    from .service import install_colocate_service, manage_colocate_service

    parser = subparsers.add_parser(
        "colocate",
        help="Run the regular control and executor processes together",
    )
    parser.add_argument("--control-config", metavar="PATH")
    parser.add_argument("--executor-config", metavar="PATH")
    parser.set_defaults(handler=run_colocate)
    actions = parser.add_subparsers(dest="colocate_command")
    install = actions.add_parser(
        "install-service", help="Install and start a native user service"
    )
    install.add_argument(
        "--control-config", metavar="PATH", default=argparse.SUPPRESS
    )
    install.add_argument(
        "--executor-config", metavar="PATH", default=argparse.SUPPRESS
    )
    install.set_defaults(handler=install_colocate_service)
    for action in ("start", "stop", "restart", "status", "uninstall"):
        operation = actions.add_parser(f"{action}-service")
        operation.set_defaults(
            handler=lambda _args, name=action: manage_colocate_service(name)
        )
    return parser


def _child_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "workgate.main", *args]


def run_colocate(args: argparse.Namespace) -> None:
    """Own two ordinary child processes; do not configure either runtime."""
    control = ["control"]
    executor = ["executor", "run"]
    if args.control_config:
        control.extend(("--config", args.control_config))
    if args.executor_config:
        executor.extend(("--config", args.executor_config))

    children: list[subprocess.Popen[Any]] = []
    if os.name == "nt":
        from ..platform.windows_job import WindowsJob

        job = WindowsJob()
        try:
            job.attach_current_process()
        except BaseException:
            job.close()
            raise
        # Keep the Job handle alive until this launcher process exits:
        # its OS-close then kills any still-running role descendants.
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(_signal: int, _frame: Any) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    try:
        popen_options = (
            {"creationflags": 0x08000000} if os.name == "nt" else {}
        )  # CREATE_NO_WINDOW for native Windows service launch
        children.append(
            subprocess.Popen(_child_command(*control), **popen_options)
        )
        children.append(
            subprocess.Popen(_child_command(*executor), **popen_options)
        )
        while True:
            for child in children:
                status = child.poll()
                if status is not None:
                    raise SystemExit(status)
            time.sleep(0.2)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        for child in children:
            if child.poll() is None:
                with suppress(ProcessLookupError):
                    child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    child.kill()
                child.wait()
