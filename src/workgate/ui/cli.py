"""Command-line registration for the optional native OpenTUI client."""

import argparse
from typing import Any

from ..app_paths import ensure_private_directory
from ..config.cli import register_config_and_setting_args, settings_from_args
from ..config.control import CONTROL_SETTING_NAMES, resolve_control_config
from ..config.role_config import use_role_config
from ..persistence import FileStateStore, use_state_store
from .runtime import run_tui
from .security import UI_API_PREFIX


def register_tui_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the native TUI command and its runtime settings."""
    parser = subparsers.add_parser(
        "tui",
        help="Launch the optional native OpenTUI client",
        description="Launch the optional native OpenTUI client.",
    )
    register_config_and_setting_args(
        parser,
        setting_names=CONTROL_SETTING_NAMES,
    )
    parser.add_argument(
        "--api-base",
        default=None,
        metavar="URL",
        help=(
            "Loopback Human UI API base. Defaults to "
            "http://127.0.0.1:<port>/api/ui."
        ),
    )
    parser.set_defaults(handler=run_tui_from_args)
    return parser


def run_tui_from_args(args: argparse.Namespace) -> None:
    """Load settings and launch OpenTUI against the loopback HTTP service."""
    settings = settings_from_args(args)
    config = resolve_control_config(settings)
    ensure_private_directory(config.state_dir)
    api_base = args.api_base or f"http://127.0.0.1:{config.port}{UI_API_PREFIX}"
    state_store = FileStateStore(lambda: config.state_dir)
    with use_role_config(config), use_state_store(state_store):
        raise SystemExit(run_tui(api_base, settings=config))
