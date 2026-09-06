"""Compose and dispatch the workgate command-line interface."""

import argparse

from .agent_bridge.cli import register_mcp_cli
from .control.cli import register_server_cli
from .jobs.cli import register_job_runner_cli
from .remote_worker.cli import register_worker_cli
from .ui.cli import register_tui_cli
from .version import format_version_info, register_version_cli


def _build_parser() -> argparse.ArgumentParser:
    """Build the root parser from domain-owned subcommand registrations."""
    parser = argparse.ArgumentParser(
        prog="workgate",
        description="Run a server, native TUI, Agent Bridge command, or remote worker.",
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
    register_server_cli(subparsers)
    register_tui_cli(subparsers)
    register_mcp_cli(subparsers)
    register_worker_cli(subparsers)
    register_version_cli(subparsers)
    register_job_runner_cli(subparsers)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse the complete argparse command tree and invoke its handler."""
    args = _build_parser().parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
