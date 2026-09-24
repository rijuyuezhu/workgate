#!/usr/bin/env python3
"""Stage the dependency-light Workgate hosted closure for pywrangler."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEST = Path(__file__).resolve().parent / "src" / "workgate"

MODULES = (
    "__init__.py",
    "app_paths.py",
    "errors.py",
    "config/__init__.py",
    "config/settings.py",
    "control/__init__.py",
    "control/config.py",
    "control/executor_transport.py",
    "control/pairing.py",
    "control/sessions.py",
    "control/state.py",
    "control/streams.py",
    "hosted/__init__.py",
    "hosted/_tool_manifest.py",
    "hosted/actor.py",
    "hosted/http.py",
    "hosted/mcp.py",
    "hosted/state_store.py",
    "persistence/__init__.py",
    "persistence/store.py",
    "protocol/__init__.py",
    "protocol/credentials.py",
    "protocol/errors.py",
    "protocol/executor.py",
    "protocol/ids.py",
    "protocol/pairing.py",
    "protocol/terminal.py",
    "utils/__init__.py",
    "utils/private_files.py",
)


def stage(dest: Path = DEFAULT_DEST) -> tuple[Path, ...]:
    """Copy the exact hosted source closure into a deployment tree."""
    source = ROOT / "src" / "workgate"
    if dest.exists():
        shutil.rmtree(dest)
    copied: list[Path] = []
    for relative in MODULES:
        src = source / relative
        if not src.is_file():
            raise FileNotFoundError(f"missing hosted source module: {relative}")
        dst = dest / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(dst)
    return tuple(copied)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()
    copied = stage(args.dest)
    print(f"staged {len(copied)} Workgate modules in {args.dest}")


if __name__ == "__main__":
    main()
