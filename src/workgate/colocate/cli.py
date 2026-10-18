"""Launch ordinary Control and Executor CLI processes on one machine."""

import argparse
from typing import Any

from ..supervisor import ChildCommand, child_command, run_parent


def register_colocate_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the same-host launcher and optional systemd installation."""
    from .service import install_colocate_service

    parser = subparsers.add_parser(
        "colocate",
        help="Run the regular control and executor processes together",
    )
    parser.add_argument("--control-config", metavar="PATH")
    parser.add_argument("--executor-config", metavar="PATH")
    parser.set_defaults(handler=run_colocate)
    actions = parser.add_subparsers(dest="colocate_command")
    install = actions.add_parser(
        "install-service", help="Install and start a systemd user service"
    )
    install.add_argument(
        "--control-config", metavar="PATH", default=argparse.SUPPRESS
    )
    install.add_argument(
        "--executor-config", metavar="PATH", default=argparse.SUPPRESS
    )
    install.set_defaults(handler=install_colocate_service)
    return parser


def _child_command(*args: str) -> list[str]:
    return child_command(*args)


def run_colocate(args: argparse.Namespace) -> None:
    """One Parent owns two ordinary role Children, with no nested supervisor."""
    control = ["control"]
    executor = ["executor", "run"]
    if args.control_config:
        control.extend(("--config", args.control_config))
    if args.executor_config:
        executor.extend(("--config", args.executor_config))

    run_parent(
        [
            ChildCommand("control", tuple(_child_command(*control))),
            ChildCommand("executor", tuple(_child_command(*executor))),
        ]
    )
