"""Compose and dispatch the workgate command-line interface."""

import argparse
import sys

from .agent_bridge.cli import register_mcp_cli
from .colocate.cli import register_colocate_cli
from .control.cli import register_control_cli
from .executor.cli import register_executor_cli
from .executor.jobs.cli import register_job_runner_cli
from .supervisor import ChildCommand, child_command, is_child, run_parent
from .version import format_version_info, register_version_cli


def _build_parser() -> argparse.ArgumentParser:
    """Build the root parser from domain-owned subcommand registrations."""
    parser = argparse.ArgumentParser(
        prog="workgate",
        description=(
            "Run colocated Workgate, a control process, executor, "
            "or Agent Bridge command."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=format_version_info(),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
    )
    register_colocate_cli(subparsers)
    register_control_cli(subparsers)
    register_mcp_cli(subparsers)
    register_executor_cli(subparsers)
    register_version_cli(subparsers)
    register_job_runner_cli(subparsers)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse the complete argparse command tree and invoke its handler."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(arguments)
    role = (
        "control"
        if args.command == "control"
        else "executor"
        if args.command == "executor" and args.executor_command == "run"
        else None
    )
    if role is not None and not is_child():
        run_parent([ChildCommand(role, tuple(child_command(*arguments)))])
        return
    args.handler(args)


if __name__ == "__main__":
    main()
