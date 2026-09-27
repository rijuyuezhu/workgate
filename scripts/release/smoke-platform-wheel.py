#!/usr/bin/env python3
"""Smoke-test an installed platform wheel without Bun or an OpenTUI sidecar."""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from workgate import __version__
from workgate.app_paths import app_paths
from workgate.config.control import resolve_control_config
from workgate.config.settings import Settings
from workgate.ui.runtime import (
    embedded_tui_payload,
    resolve_tui_command,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify an installed embedded OpenTUI platform wheel",
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    return parser


def _require_private_runtime(path: Path, cache_dir: Path) -> None:
    resolved_path = path.resolve()
    resolved_cache = cache_dir.resolve()
    try:
        resolved_path.relative_to(resolved_cache)
    except ValueError as exc:
        raise RuntimeError(
            f"embedded OpenTUI escaped the private cache directory: {resolved_path}"
        ) from exc
    if not path.is_file():
        raise RuntimeError(f"embedded OpenTUI was not materialized: {path}")
    if os.name != "nt":
        directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
        executable_mode = stat.S_IMODE(path.stat().st_mode)
        if directory_mode & 0o077:
            raise RuntimeError(
                f"embedded OpenTUI directory is not private: {oct(directory_mode)}"
            )
        if not executable_mode & stat.S_IXUSR:
            raise RuntimeError("embedded OpenTUI is not owner-executable")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(args.work_dir)

    if shutil.which("bun") is not None:
        raise RuntimeError(
            "Bun must be absent from PATH during platform-wheel smoke"
        )
    payload = embedded_tui_payload()
    if payload is None or not payload.is_file():
        raise RuntimeError(
            "installed wheel does not contain an embedded OpenTUI payload"
        )

    settings = resolve_control_config(
        Settings(state_dir=args.state_dir, ui_tui_command=None)
    )
    first = resolve_tui_command(settings)
    second = resolve_tui_command(settings)
    if first != second or len(first) != 1:
        raise RuntimeError("embedded OpenTUI resolution is not stable")
    runtime = Path(first[0])
    _require_private_runtime(runtime, app_paths().ui_runtime_dir)

    try:
        completed = subprocess.run(
            [str(runtime), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("embedded OpenTUI could not execute") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"embedded OpenTUI --version failed with {completed.returncode}: {detail}"
        )

    print(
        json.dumps(
            {
                "package_version": __version__,
                "payload": str(payload),
                "runtime": str(runtime),
                "runtime_version": completed.stdout.strip(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"platform wheel smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
