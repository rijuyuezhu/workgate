"""Launch ordinary Control and Executor CLI processes on one machine."""

import argparse
import signal
import subprocess
import sys
import time
from typing import Any


def register_colocate_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the optional same-host process launcher."""
    parser = subparsers.add_parser(
        "colocate",
        help="Run the regular control and executor processes together",
    )
    parser.add_argument("--control-config", metavar="PATH")
    parser.add_argument("--executor-config", metavar="PATH")
    parser.set_defaults(handler=run_colocate)
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
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(_signal: int, _frame: Any) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    try:
        children.append(subprocess.Popen(_child_command(*control)))
        children.append(subprocess.Popen(_child_command(*executor)))
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
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
