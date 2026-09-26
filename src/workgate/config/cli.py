"""Shared argparse contracts for configuration-backed CLI commands."""

import argparse
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from .settings import Settings, load_settings
from .surface import cli_overrides_from_args, register_setting_cli_args


def register_config_and_setting_args(
    parser: argparse.ArgumentParser,
    *,
    setting_names: Collection[str] | None = None,
    exclude_setting_names: Collection[str] = (),
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
        include_names=setting_names,
        exclude_names=exclude_setting_names,
    )
    parser.set_defaults(
        _workgate_default_config_path=default_config_path,
        _workgate_setting_defaults=dict(setting_defaults or {}),
    )
    if setting_names is not None:
        parser.set_defaults(_workgate_setting_names=frozenset(setting_names))


def settings_from_args(args: argparse.Namespace) -> Settings:
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
    )
    return settings
