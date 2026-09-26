#!/usr/bin/env python3
"""Stage the dependency-light Workgate hosted closure for pywrangler."""

import argparse
import os
import shutil
from contextlib import suppress
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
    "config/control.py",
    "config/role_config.py",
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


def _same_contents(left: Path, right: Path) -> bool:
    if not right.is_file():
        return False
    if left.stat().st_size != right.stat().st_size:
        return False
    return left.read_bytes() == right.read_bytes()


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _same_contents(src, dst):
        return
    staged = dst.with_name(f".{dst.name}.workgate-stage-{os.getpid()}")
    try:
        shutil.copy2(src, staged)
        staged.replace(dst)
    finally:
        staged.unlink(missing_ok=True)


def _remove_stale(dest: Path, expected: set[Path]) -> None:
    if not dest.exists():
        return
    files = tuple(path for path in dest.rglob("*") if path.is_file())
    for path in files:
        if path.relative_to(dest) not in expected:
            path.unlink()
    directories = sorted(
        (path for path in dest.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        with suppress(OSError):
            path.rmdir()


def stage(dest: Path = DEFAULT_DEST) -> tuple[Path, ...]:
    """Copy the exact hosted source closure without removing the live package."""
    source = ROOT / "src" / "workgate"
    expected = {Path(relative) for relative in MODULES}
    copied: list[Path] = []
    for relative in MODULES:
        src = source / relative
        if not src.is_file():
            raise FileNotFoundError(f"missing hosted source module: {relative}")
        dst = dest / relative
        _atomic_copy(src, dst)
        copied.append(dst)
    _remove_stale(dest, expected)
    return tuple(copied)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()
    copied = stage(args.dest)
    print(f"staged {len(copied)} Workgate modules in {args.dest}")


if __name__ == "__main__":
    main()
