"""Verified NT-handle directory enumeration for Windows Skill discovery."""

from collections.abc import Callable
from pathlib import Path

from .archive import list_tree, verify_directory


def require_skill_root(directory: Path) -> bool:
    """Reject junctions; a missing Skill root means there are no Skills."""
    try:
        verify_directory(directory)
    except FileNotFoundError:
        return False
    return True


def candidate_directories(
    root: Path, *, limit: int, excluded: frozenset[str]
) -> tuple[list[str], int, bool]:
    """List bounded subdirectories without following reparse points."""
    entries, truncated = list_tree(root, recursive=False, max_entries=limit)
    return (
        [
            p.name
            for p, kind, *_ in entries
            if kind == "dir" and p.name not in excluded
        ],
        len(entries),
        truncated,
    )


def related_files(
    root: Path,
    *,
    entry_limit: int,
    file_limit: int,
    path_byte_limit: int,
    ignored_dirs: frozenset[str],
    validate_path: Callable[[str], Path],
) -> tuple[list[str], list[str], int, int]:
    """List bounded regular asset paths without traversing links or junctions."""
    warnings: list[str] = []
    try:
        items, truncated = list_tree(
            root, recursive=True, max_entries=entry_limit
        )
    except OSError as exc:
        return [], [f"Could not scan related files: {exc}"], 0, 0

    result: list[str] = []
    path_bytes = 0
    for path, kind, *_ in items:
        if kind != "file":
            continue
        relative_path = path.relative_to(root)
        relative = relative_path.as_posix()
        if relative == "SKILL.md" or any(
            segment in ignored_dirs for segment in relative_path.parts[:-1]
        ):
            continue
        try:
            validate_path(relative)
        except ValueError as exc:
            warnings.append(f"Skipping related path {relative}: {exc}")
            continue
        length = len(relative.encode("utf-8"))
        if len(result) >= file_limit or path_bytes + length > path_byte_limit:
            warnings.append("Related file list truncated by budget")
            break
        result.append(relative)
        path_bytes += length
    if truncated:
        warnings.append(
            f"Related file scan stopped after {entry_limit} entries"
        )
    return sorted(result), warnings, len(items), path_bytes
