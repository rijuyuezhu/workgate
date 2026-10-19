"""Compare-and-swap updates to the config file actually owned by a running role.

Saving changes only the file. The Child's Settings snapshot remains untouched.
"""

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import ValidationError

from ..utils.private_files import atomic_write_private_bytes, private_file_lock
from .roles import CONTROL_SETTING_NAMES, EXECUTOR_SETTING_NAMES
from .settings import _CONFIG_PATH_FIELDS, Settings, _default_setting_values


class ConfigSnapshot(Protocol):
    """Only the attributes file editing needs from a running role snapshot."""

    @property
    def role(self) -> Literal["control", "executor"]: ...

    @property
    def config_path(self) -> Path | None: ...

    @property
    def env_values(self) -> dict[str, Any]: ...

    @property
    def cli_values(self) -> dict[str, Any]: ...


class ConfigEditError(Exception):
    """A safe, credential-free error exposed by the edit API."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _target(snapshot: ConfigSnapshot) -> Path:
    path = snapshot.config_path
    if path is None:
        raise ConfigEditError("NoConfigFile", 409)
    if path.resolve() != path.absolute():
        raise ConfigEditError("UnsafeConfigPath", 403)
    try:
        directory = path.parent.stat()
        info = path.lstat()
        if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISREG(
            info.st_mode
        ):
            raise ConfigEditError("UnsafeConfigPath", 403)
        if hasattr(os, "getuid") and (
            directory.st_uid != os.getuid() or info.st_uid != os.getuid()
        ):
            raise ConfigEditError("ConfigNotOwned", 403)
        if info.st_mode & 0o022 or not os.access(path.parent, os.W_OK):
            raise ConfigEditError("UnsafeConfigPermissions", 403)
    except OSError as exc:
        raise ConfigEditError("ConfigNotWritable", 403) from exc
    return path


def revision_for(snapshot: ConfigSnapshot) -> str | None:
    try:
        return hashlib.sha256(_target(snapshot).read_bytes()).hexdigest()
    except ConfigEditError, OSError:
        return None


def _mapping(content: bytes) -> dict[str, Any]:
    try:
        data = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise ConfigEditError("InvalidConfigFile", 409) from exc
    if data is None:
        return {}
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise ConfigEditError("InvalidConfigFile", 409)
    if data.keys() - Settings.model_fields.keys():
        raise ConfigEditError("InvalidConfigFile", 409)
    return data


def _validate(snapshot: ConfigSnapshot, candidate: dict[str, Any]) -> None:
    role_names = (
        CONTROL_SETTING_NAMES
        if snapshot.role == "control"
        else EXECUTOR_SETTING_NAMES
    )
    file_values = {k: v for k, v in candidate.items() if k in role_names}
    for key in file_values.keys() & _CONFIG_PATH_FIELDS:
        value = file_values[key]
        if (
            value is not None
            and not Path(
                os.path.expandvars(os.path.expanduser(str(value)))
            ).is_absolute()
        ):
            raise ConfigEditError("InvalidConfigValue")
    defaults = _default_setting_values()
    try:
        Settings(**(defaults | file_values))  # A valid file on its own.
        # Also validate effective role startup precedence without applying it.
        Settings(
            **(
                defaults
                | file_values
                | snapshot.env_values
                | snapshot.cli_values
            )
        )
    except (ValidationError, ValueError, TypeError) as exc:
        # Pydantic errors can contain submitted secrets: never forward details.
        raise ConfigEditError("InvalidConfigValue") from exc


def _encode(
    path: Path, old: bytes, candidate: dict[str, Any], updates: dict[str, Any]
) -> bytes:
    if path.suffix == ".json":
        return (
            json.dumps(candidate, indent=2, ensure_ascii=False) + "\n"
        ).encode()
    # Preserve ordinary YAML comments/formatting for simple top-level scalar
    # replacements. Structural edits safely fall back to canonical YAML.
    if updates and all(k in _mapping(old) for k in updates):
        lines = old.decode().splitlines(keepends=True)
        remaining = set(updates)
        for i, line in enumerate(lines):
            if not line or line[0].isspace() or line.lstrip().startswith("#"):
                continue
            key, sep, rest = line.partition(":")
            if (
                not sep
                or key not in remaining
                or not rest.strip()
                or rest.lstrip().startswith(("|", ">"))
            ):
                continue
            if " #" in rest:
                # Keep trailing comment only when it follows a single-line scalar.
                old_value, comment = rest.rstrip("\n").split(" #", 1)
                if "\n" in old_value:
                    continue
                suffix = " #" + comment
            else:
                suffix = ""
            dumped = yaml.safe_dump(
                updates[key], default_flow_style=True, allow_unicode=True
            ).splitlines()
            if (
                not dumped
                or len(dumped) > 2
                or (len(dumped) == 2 and dumped[1] != "...")
            ):
                continue
            scalar = dumped[0]
            if scalar.startswith(("{", "[")):
                continue
            lines[i] = f"{key}: {scalar}{suffix}\n"
            remaining.remove(key)
        if not remaining:
            preserved = "".join(lines).encode("utf-8")
            # A seemingly simple scalar rewrite can still change YAML meaning
            # (e.g. duplicate keys or quoted '#'). Never publish that result.
            if _mapping(preserved) == candidate:
                return preserved
    return yaml.safe_dump(
        candidate, sort_keys=False, allow_unicode=True
    ).encode("utf-8")


def save_config(
    snapshot: ConfigSnapshot, *, revision: str, changes: dict[str, Any]
) -> str:
    """Save requested set/unset operations under a file lock, or fail closed."""
    if (
        not isinstance(revision, str)
        or len(revision) != 64
        or not isinstance(changes, dict)
        or not changes
    ):
        raise ConfigEditError("InvalidEdit")
    names = (
        CONTROL_SETTING_NAMES
        if snapshot.role == "control"
        else EXECUTOR_SETTING_NAMES
    )
    if any(
        name not in names
        or not isinstance(action, dict)
        or action.get("op") not in {"set", "remove"}
        or (action["op"] == "set" and set(action) != {"op", "value"})
        or (action["op"] == "remove" and set(action) != {"op"})
        for name, action in changes.items()
    ):
        raise ConfigEditError("InvalidEdit")
    path = _target(snapshot)
    try:
        with private_file_lock(
            path.with_name(f".{path.name}.lock"), timeout_s=5
        ):
            _target(snapshot)  # Re-check after waiting for a competing writer.
            old = path.read_bytes()
            if hashlib.sha256(old).hexdigest() != revision:
                raise ConfigEditError("RevisionConflict", 409)
            candidate = _mapping(old)
            for name, action in changes.items():
                if action["op"] == "remove":
                    candidate.pop(name, None)
                else:
                    candidate[name] = action["value"]
            _validate(snapshot, candidate)
            updated = _encode(
                path,
                old,
                candidate,
                {k: v["value"] for k, v in changes.items() if v["op"] == "set"},
            )
            if updated != old:
                # A private last-known-good file for later explicit Apply/rollback.
                backup = path.with_name(f"{path.name}.bak")
                atomic_write_private_bytes(backup, old)
                atomic_write_private_bytes(path, updated)
            return hashlib.sha256(updated).hexdigest()
    except ConfigEditError:
        raise
    except (OSError, TimeoutError) as exc:
        raise ConfigEditError("ConfigWriteFailed", 503) from exc


def validate_apply(snapshot: ConfigSnapshot, *, revision: str) -> None:
    """Check the exact saved revision and full role Settings before restart.

    This is a read-only gate; Parent owns restart and recovery, not the UI.
    """
    if not isinstance(revision, str) or len(revision) != 64:
        raise ConfigEditError("InvalidEdit")
    path = _target(snapshot)
    try:
        with private_file_lock(
            path.with_name(f".{path.name}.lock"), timeout_s=5
        ):
            _target(snapshot)
            current = path.read_bytes()
            if hashlib.sha256(current).hexdigest() != revision:
                raise ConfigEditError("RevisionConflict", 409)
            _validate(snapshot, _mapping(current))
    except ConfigEditError:
        raise
    except OSError, TimeoutError:
        raise ConfigEditError("ConfigReadFailed", 503) from None
