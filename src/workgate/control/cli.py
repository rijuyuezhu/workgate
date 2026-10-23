"""Command-line registration for the control process."""

import argparse
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from ..app_paths import ensure_private_directory
from ..config.cli import (
    register_config_and_setting_args,
    settings_and_source_from_args,
)
from ..config.control import CONTROL_SETTING_NAMES
from ..config.inspection import capture_startup_config
from ..config.settings import configure_settings
from ..supervisor import supervise_cli_role
from ..utils.private_files import private_file_lock
from .http.app import run_http
from .mcp.app import run_mcp
from .runtime import build_control_runtime
from .server import configure_runtime_logging


def register_control_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the production control-process entrypoint."""
    parser = subparsers.add_parser(
        "control",
        help="Run the Workgate control process",
        description="Run the Workgate control process.",
    )
    register_config_and_setting_args(
        parser,
        setting_names=CONTROL_SETTING_NAMES,
    )
    parser.set_defaults(handler=run_control_from_args)
    from .service import install_control_service, manage_control_service

    actions = parser.add_subparsers(dest="control_command")
    install = actions.add_parser(
        "install-service",
        help="Install and start Control as a native user service",
    )
    install.add_argument("--config", default=argparse.SUPPRESS, metavar="PATH")
    install.set_defaults(handler=install_control_service)
    for action in ("start", "stop", "restart", "status", "uninstall"):
        operation = actions.add_parser(f"{action}-service")
        operation.set_defaults(
            handler=lambda _args, name=action: manage_control_service(name)
        )
    return parser


class ControlAlreadyRunningError(RuntimeError):
    """Raised when another process already owns the control state root."""


@contextmanager
def control_run_lock(state_dir: Path) -> Generator[None]:
    """Hold one non-blocking single-writer lock for the control state root."""
    control_dir = ensure_private_directory(state_dir / "control")
    stack = ExitStack()
    try:
        stack.enter_context(
            private_file_lock(control_dir / "run.lock", timeout_s=0)
        )
    except TimeoutError as exc:
        raise ControlAlreadyRunningError(
            "control state is already active in another process"
        ) from exc
    with stack:
        yield


def _dispatch_control(settings: Any) -> None:
    """Build one control runtime and launch the selected transport adapter."""
    runtime = build_control_runtime(settings)
    match runtime.config.mode:
        case "http":
            run_http(runtime=runtime)
        case "mcp" | "stdio":
            run_mcp(runtime=runtime)
        case _:
            raise SystemExit(f"Unsupported mode: {runtime.config.mode}")


def run_control_from_args(args: argparse.Namespace) -> None:
    """Load production control settings without acquiring executor machine authority."""
    if supervise_cli_role("control", args):
        return
    settings, source = settings_and_source_from_args(args)
    capture_startup_config("control", args, settings, source)
    ensure_private_directory(settings.state_dir)
    ensure_private_directory(settings.data_dir)
    ensure_private_directory(settings.audit_log_path.parent)
    try:
        with control_run_lock(settings.state_dir):
            configure_settings(settings)
            configure_runtime_logging(settings.log_level)
            _dispatch_control(settings)
    except ControlAlreadyRunningError as exc:
        raise SystemExit(str(exc)) from exc
