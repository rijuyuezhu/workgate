"""Version reporting and CLI registration helpers."""

import argparse
import importlib.metadata as importlib_metadata
import platform
import sys
from typing import Any

from . import __version__


def package_version() -> str:
    """Return the installed distribution version, falling back to the source version."""
    try:
        return importlib_metadata.version("workgate")
    except importlib_metadata.PackageNotFoundError:
        return __version__


def version_info() -> dict[str, Any]:
    """Return structured runtime and package version metadata."""
    return {
        "version": __version__,
        "package_version": package_version(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }


def diagnostic_version_info() -> dict[str, Any]:
    """Report optional provenance only on authenticated diagnostic surfaces."""
    from .build_info import build_info

    return {**version_info(), **build_info()}


def format_version_info(info: dict[str, Any] | None = None) -> str:
    """Return the human-readable CLI version string."""
    data = version_info() if info is None else info
    if data.get("package_version") and data.get("package_version") != data.get(
        "version"
    ):
        return f"workgate {data['version']} (package {data['package_version']})"
    return f"workgate {data['version']}"


def register_version_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the detailed version-reporting subcommand."""
    parser = subparsers.add_parser(
        "version",
        help="Print workgate version information",
    )
    parser.set_defaults(handler=print_version_from_args)
    return parser


def print_version_from_args(args: argparse.Namespace) -> None:
    """Print detailed version information."""
    del args
    print(format_version_info())
