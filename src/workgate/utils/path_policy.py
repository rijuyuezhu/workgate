"""Pure path-policy primitives shared across runtime ownership layers."""

import os
from pathlib import Path

from ..errors import PathNotFoundError


def resolve_path_with_policy(
    path: str | Path,
    *,
    workspace_root: Path,
    allow_full_control: bool,
    path_denylist: tuple[str, ...],
    must_exist: bool = False,
    allow_missing_parent: bool = True,
    follow_final_symlink: bool = True,
) -> Path:
    """Resolve a path from explicit workspace-boundary policy values."""
    root = workspace_root.resolve()
    raw = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not raw.is_absolute():
        raw = root / raw
    if follow_final_symlink:
        resolved = (
            Path(os.path.abspath(raw))
            if allow_full_control
            else raw.resolve(strict=False)
        )
    else:
        resolved_parent = raw.parent.resolve(strict=False)
        resolved = resolved_parent / raw.name if raw.name else resolved_parent
    if not allow_full_control:
        boundary = resolved if follow_final_symlink else resolved.parent
        try:
            boundary.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Path escapes workspace: {path}") from exc

    lower = str(resolved).lower()
    for denied in path_denylist:
        if denied and denied.lower() in lower:
            raise PermissionError(f"Path is denylisted: {path}")

    exists = (
        resolved.exists() if follow_final_symlink else os.path.lexists(resolved)
    )
    if must_exist and not exists:
        raise PathNotFoundError(resolved)
    if not allow_missing_parent and not resolved.parent.exists():
        raise PathNotFoundError(resolved.parent)
    return resolved


def relative_display_from_root(path: Path, root: Path) -> str:
    """Render a lexical API path relative to an explicit workspace root."""
    resolved_root = root.resolve()
    candidate = path if path.is_absolute() else resolved_root / path
    lexical = Path(os.path.abspath(candidate))
    try:
        return lexical.relative_to(resolved_root).as_posix()
    except ValueError:
        return lexical.as_posix()
