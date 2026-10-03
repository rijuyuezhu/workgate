"""Pure path-resolution primitives shared across runtime ownership layers."""

import os
from pathlib import Path

from ..errors import PathNotFoundError


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


def relative_display_from_base(path: Path, base: Path) -> str:
    """Render a lexical API path relative to one display base when possible."""
    resolved_root = base.resolve(strict=False)
    candidate = path if path.is_absolute() else resolved_root / path
    lexical = Path(os.path.abspath(candidate))
    try:
        return lexical.relative_to(resolved_root).as_posix()
    except ValueError:
        return lexical.as_posix()
