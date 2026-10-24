"""Launch ordinary Control and Executor CLI processes on one machine."""

import argparse
from typing import Any

from ..supervisor import ChildCommand, child_command, run_parent


def register_colocate_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the same-host launcher and native user-service operations."""
    from .service import install_colocate_service, manage_colocate_service

    parser = subparsers.add_parser(
        "colocate",
        help="Run the regular control and executor processes together",
    )
    parser.add_argument("--control-config", metavar="PATH")
    parser.add_argument("--executor-config", metavar="PATH")
    parser.set_defaults(handler=run_colocate)
    actions = parser.add_subparsers(dest="colocate_command")
    from .first_run import setup_colocate

    setup = actions.add_parser(
        "setup",
        help="Initialize, locally pair and install Colocate user service",
    )
    setup.add_argument(
        "--control-config", metavar="PATH", default=argparse.SUPPRESS
    )
    setup.add_argument(
        "--executor-config", metavar="PATH", default=argparse.SUPPRESS
    )
    setup.add_argument("--name", default=None)
    setup.set_defaults(handler=setup_colocate)

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
