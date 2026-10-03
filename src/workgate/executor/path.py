"""Executor path, temporary-file, and text-size helpers."""

import os
import time
from pathlib import Path

from ..app_paths import ensure_private_directory
from ..errors import PathNotFoundError


def resolve_default_workdir(path: Path) -> Path:
    """Return the live default directory, falling back to the filesystem root."""
    resolved = path.resolve(strict=False)
    return resolved if resolved.is_dir() else Path("/").resolve(strict=False)


def resolve_path(
    path: str | Path,
    *,
    base: Path,
    must_exist: bool = False,
    allow_missing_parent: bool = True,
    follow_final_symlink: bool = True,
) -> Path:
    """Resolve one user path relative to an explicit base directory."""
    root = base.resolve(strict=False)
    raw = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not raw.is_absolute():
        raw = root / raw
    if follow_final_symlink:
        resolved = raw.resolve(strict=False)
    else:
        resolved_parent = raw.parent.resolve(strict=False)
        resolved = resolved_parent / raw.name if raw.name else resolved_parent

    exists = (
        resolved.exists() if follow_final_symlink else os.path.lexists(resolved)
    )
    if must_exist and not exists:
        raise PathNotFoundError(resolved)
    if not allow_missing_parent and not resolved.parent.exists():
        raise PathNotFoundError(resolved.parent)
    return resolved


def display_path(path: Path, base: Path) -> str:
    """Render a path relative to a display base when possible."""
    resolved_base = base.resolve(strict=False)
    candidate = path if path.is_absolute() else resolved_base / path
    lexical = Path(os.path.abspath(candidate))
    try:
        return lexical.relative_to(resolved_base).as_posix()
    except ValueError:
        return lexical.as_posix()


def temp_dir(directory: Path) -> Path:
    """Create and return one explicit executor-owned scratch directory."""
    return ensure_private_directory(directory)


def prune_temp_dir(
    *,
    max_files: int,
    max_bytes: int,
    directory: Path,
    minimum_age_s: float = 0.0,
    protected_paths: frozenset[Path] = frozenset(),
    excluded_name_prefixes: tuple[str, ...] = (
        "remote-transfer-",
        ".remote-transfer-",
    ),
) -> None:
    """Remove over-budget scratch files without crossing durable namespaces."""
    path = temp_dir(directory)
    try:
        files = [item for item in path.iterdir() if item.is_file()]
    except OSError:
        return

    try:
        protected = {item.resolve(strict=False) for item in protected_paths}
    except OSError:
        return
    entries: list[tuple[float, int, Path]] = []
    for item in files:
        try:
            resolved = item.resolve(strict=False)
            stat = item.stat()
        except OSError:
            continue
        if resolved in protected or item.name.startswith(
            excluded_name_prefixes
        ):
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
