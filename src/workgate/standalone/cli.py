"""CLI registration for the offline two-process standalone mode."""

from __future__ import annotations

import argparse
from typing import Any

from ..config.cli import register_config_and_setting_args, settings_from_args
from .supervisor import StandaloneAlreadyRunningError, run_standalone

_STANDALONE_FORCED_SETTING_NAMES = frozenset(
    {
        "mode",
        "host",
        "base_url",
        "auth_mode",
        "auth_bypass_localhost",
        "oauth_issuer",
        "oauth_resource",
    }
)


def register_standalone_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the offline standalone supervisor entrypoint."""
    parser = subparsers.add_parser(
        "standalone",
        help="Run local control and executor as separate supervised processes",
        description=(
            "Run Workgate fully offline with separate loopback control and "
            "executor processes."
        ),
    )
    register_config_and_setting_args(
        parser,
        exclude_setting_names=_STANDALONE_FORCED_SETTING_NAMES,
    )
    parser.set_defaults(handler=run_standalone_from_args)
    return parser


def run_standalone_from_args(args: argparse.Namespace) -> None:
    """Resolve one user config without installing ambient parent settings."""
    settings = settings_from_args(args, configure=False)
    try:
        run_standalone(settings)
    except StandaloneAlreadyRunningError as exc:
        raise SystemExit(str(exc)) from exc
