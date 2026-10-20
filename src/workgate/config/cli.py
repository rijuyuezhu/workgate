"""Shared argparse contracts for configuration-backed CLI commands."""

import argparse
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

from .settings import LoadedConfigFile, Settings, load_settings
from .surface import cli_overrides_from_args, register_setting_cli_args


def register_config_and_setting_args(
    parser: argparse.ArgumentParser,
    *,
    setting_names: Collection[str],
    cli_setting_names: Collection[str] | None = None,
    default_config_path: str | Path | None = None,
    setting_defaults: Mapping[str, Any] | None = None,
) -> None:
    """Register config-file selection and role-appropriate Settings overrides."""
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "Path to optional YAML config file. Overrides WORKGATE_CONFIG. "
            "This selects the config file and is not itself a Settings field."
        ),
    )
    register_setting_cli_args(
        parser,
        include_names=cli_setting_names
        if cli_setting_names is not None
        else setting_names,
    )
    parser.set_defaults(
        _workgate_default_config_path=default_config_path,
        _workgate_setting_defaults=dict(setting_defaults or {}),
        _workgate_setting_names=frozenset(setting_names),
    )


def settings_from_args(
    args: argparse.Namespace,
    *,
    on_file_read: Callable[[LoadedConfigFile], None] | None = None,
) -> Settings:
    """Load CLI-selected settings without installing process-wide runtime state."""
    setting_names = getattr(args, "_workgate_setting_names", None)
    settings = load_settings(
        args.config,
        cli_overrides_from_args(args),
        setting_names=setting_names,
        default_config_path=getattr(
            args, "_workgate_default_config_path", None
        ),
        default_overrides=getattr(args, "_workgate_setting_defaults", None),
        on_file_read=on_file_read,
    )
    return settings


def settings_and_source_from_args(
    args: argparse.Namespace,
) -> tuple[Settings, LoadedConfigFile]:
    """Load runtime settings and their file revision from the same read."""
    captured: list[LoadedConfigFile] = []
    settings = settings_from_args(args, on_file_read=captured.append)
    return settings, captured[0]
