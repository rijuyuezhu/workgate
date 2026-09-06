"""Command-line registration for the control server adapters."""

import argparse
from typing import Any

from ..config.cli import register_config_and_setting_args, settings_from_args
from .http.app import run_http
from .mcp.app import run_mcp
from .runtime import build_control_runtime


def register_server_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the explicit server command and its Settings overrides."""
    parser = subparsers.add_parser(
        "server",
        help="Run the configured MCP or REST server",
        description="Run the configured MCP or REST server.",
    )
    register_config_and_setting_args(parser)
    parser.set_defaults(handler=run_server_from_args)
    return parser


def run_server_from_args(args: argparse.Namespace) -> None:
    """Load settings and launch the selected control server adapter."""
    settings = settings_from_args(args, configure=True)
    runtime = build_control_runtime(settings)
    match settings.mode:
        case "http":
            run_http(runtime=runtime)
        case "mcp" | "stdio":
            run_mcp(runtime=runtime)
        case "both":
            raise SystemExit(
                "mode=both is reserved; run separate mcp/http processes for now"
            )
        case _:
            raise SystemExit(f"Unsupported mode: {settings.mode}")
