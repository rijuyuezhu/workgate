"""Executor path, temporary-file, and text-size helpers."""

import time
from pathlib import Path

from ..app_paths import ensure_private_directory
from ..utils.path_policy import (
    relative_display_from_root as relative_display_from_root,
)
from ..utils.path_policy import (
    resolve_path_with_policy as resolve_path_with_policy,
)


def temp_dir(directory: Path) -> Path:
    """Create and return one explicit executor-owned scratch directory."""
    return ensure_private_directory(directory)


def prune_temp_dir(
    *,
    max_files: int,
    max_bytes: int,
    directory: Path,
    minimum_age_s: float = 0.0,
) -> None:
    """Remove over-budget temporary files from one explicit scratch directory."""
    path = temp_dir(directory)
    try:
        files = [item for item in path.iterdir() if item.is_file()]
    except OSError:
        return

    entries: list[tuple[float, int, Path]] = []
    for item in files:
        try:
            stat = item.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, item))

    entries.sort(reverse=True)
    total_bytes = 0
    now = time.time()
    for index, (modified, size, item) in enumerate(entries):
        total_bytes += size
        if index < max(0, max_files) and total_bytes <= max(0, max_bytes):
            continue
        if now - modified < max(0.0, minimum_age_s):
            continue
        try:
            item.unlink()
        except OSError:
            continue


def assert_text_input_size(label: str, text: str, limit: int) -> None:
    """Reject oversized text payloads against one explicit executor limit."""
    max_bytes = max(1, int(limit))
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise ValueError(
            f"Refusing {label} of {size} bytes; max is {max_bytes}"
        )
