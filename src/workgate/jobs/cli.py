"""Private argparse registration for durable tracked-job runner attempts."""

import argparse
from typing import Any

from ..executor.bounded_runner import register_bounded_runner_cli
from .runner import configure_job_runner_parser, run_job_runner_from_args


def register_job_runner_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register private bounded and durable process-runner subcommands."""
    register_bounded_runner_cli(subparsers)
    parser = subparsers.add_parser(
        "job-runner",
        help="Run one durable job attempt (internal)",
        description="Run one durable tracked-job attempt. Internal interface.",
    )
    configure_job_runner_parser(parser)
    parser.set_defaults(handler=run_job_runner_from_args)
    return parser
