"""Small shared helpers for systemd user units."""

import shlex
from pathlib import Path


def systemd_quote(value: str, *, escape_dollar: bool = True) -> str:
    """Quote one literal systemd unit argument."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if escape_dollar:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def systemd_user_unit_path(unit_paths: str, name: str) -> Path:
    """Find the persistent user-unit directory reported by systemd."""
    try:
        candidates = shlex.split(unit_paths.strip())
    except ValueError as exc:
        raise ValueError("systemd returned an invalid user unit path") from exc
    for raw_path in candidates:
        path = Path(raw_path)
        if (
            path.is_absolute()
            and path.name == "user"
            and path.parent.name == "systemd"
        ):
            return path / name
    raise ValueError(
        "systemd user manager did not expose a persistent user unit path"
    )
