"""Generation-fenced recovery for the two restart-critical Control registries."""

import contextlib
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..persistence import StateStore

_GENERATION_RE = re.compile(r"[0-9a-f]{32}\Z")


def _generation(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _GENERATION_RE.fullmatch(value):
        raise ValueError("invalid control registry generation")
    return value


def load_registry[T](
    store: StateStore,
    path: Path,
    validate: Callable[[Any], T],
) -> T:
    """Trust the validated primary, or only a same-generation validated backup.

    All callers hold the primary StateStore transaction lock. A legacy, valid
    generation-less primary is upgraded once; missing old state starts empty.
    """
    backup_path = path.with_name(path.name + ".bak")
    marker_path = path.with_name(path.name + ".generation")
    primary_error: Exception | None = None
    primary: Any = None
    result: T | None = None
    try:
        primary = store.read_json(path)
        if primary is not None:
            result = validate(primary)
            if not isinstance(primary, dict):
                raise ValueError("control registry must be a JSON object")
            _generation(primary.get("generation"))
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        result = None
        primary_error = exc

    if result is not None and isinstance(primary, dict):
        generation = _generation(primary.get("generation"))
        if generation is None:
            # Valid legacy state remains authoritative if mirror creation fails.
            with contextlib.suppress(OSError):
                write_registry(store, path, primary)
        else:
            # The primary is the authority even after an interrupted write.
            # Repair its mirror and marker if stale, corrupt, or missing.
            try:
                marker = store.read_json(marker_path)
                backup = store.read_json(backup_path)
                needs_repair = (
                    not isinstance(marker, dict)
                    or marker.get("generation") != generation
                    or backup != primary
                )
            except OSError, ValueError, TypeError:
                needs_repair = True
            if needs_repair:
                with contextlib.suppress(OSError):
                    store.write_json(marker_path, {"generation": generation})
                    store.write_json(backup_path, primary)
        return result

    try:
        marker = store.read_json(marker_path)
        backup = store.read_json(backup_path)
        if not isinstance(marker, dict) or backup is None:
            raise ValueError("missing recovery fence or backup")
        expected = _generation(marker.get("generation"))
        if expected is None or not isinstance(backup, dict):
            raise ValueError("invalid recovery fence or backup")
        if _generation(backup.get("generation")) != expected:
            raise ValueError("stale control registry backup")
        recovered = validate(backup)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        # Truly first-run state only when none of the three files exists.
        if (
            primary is None
            and primary_error is None
            and not any(
                candidate.exists() or candidate.is_symlink()
                for candidate in (path, backup_path, marker_path)
            )
        ):
            return validate(None)
        raise RuntimeError(f"Unrecoverable control registry: {path}") from (
            primary_error or exc
        )
    store.write_json(path, backup)
    return recovered


def write_registry(
    store: StateStore, path: Path, value: dict[str, Any]
) -> None:
    """Fence the next generation before publishing primary, then its mirror."""
    generation = secrets.token_hex(16)
    payload = {**value, "generation": generation}
    store.write_json(
        path.with_name(path.name + ".generation"), {"generation": generation}
    )
    store.write_json(path, payload)
    # The committed primary is authoritative even if this repair write fails.
    with contextlib.suppress(OSError):
        store.write_json(path.with_name(path.name + ".bak"), payload)
