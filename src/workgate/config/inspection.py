"""Read-only provenance for the configuration actually selected by each Child.

Snapshots are process-local, not another configuration source. The running
Settings instance supplies effective values; current disk contents are only
an editable-file preview and are never applied here.
"""

import hashlib
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_origin

import yaml

from .. import __version__
from ..app_paths import app_paths
from .cli import cli_overrides_from_args
from .roles import CONTROL_SETTING_NAMES, EXECUTOR_SETTING_NAMES
from .settings import Settings, env_overrides, read_config_file
from .surface import SETTING_SPECS

Role = Literal["control", "executor"]


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    return str(value)


def _file_digest(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class StartupConfigInspection:
    role: Role
    settings: Settings
    config_path: Path | None
    cli_values: dict[str, Any]
    env_values: dict[str, Any]
    startup_file_digest: str | None
    startup_file_names: frozenset[str]
    startup_at: float

    def describe(self) -> dict[str, Any]:
        """Reveal configuration to an independently authenticated full admin."""
        role_names = (
            CONTROL_SETTING_NAMES
            if self.role == "control"
            else EXECUTOR_SETTING_NAMES
        )
        file_values: dict[str, Any] = {}
        file_error = None
        try:
            file_values = read_config_file(
                self.config_path, setting_names=role_names
            )
        except OSError, ValueError, TypeError, yaml.YAMLError:
            file_error = "Configuration file is unavailable or invalid"
        try:
            disk_digest = _file_digest(self.config_path)
        except OSError:
            disk_digest = None
            file_error = "Configuration file is unavailable or invalid"
        effective = self.settings.model_dump(mode="json")
        fields = []
        for spec in SETTING_SPECS:
            name = spec.name
            if name not in role_names:
                continue
            source = (
                "cli"
                if name in self.cli_values
                else "env"
                if name in self.env_values
                else "file"
                if name in self.startup_file_names
                else "default"
            )
            fields.append(
                {
                    "name": name,
                    "section": spec.section,
                    "description": spec.help,
                    "choices": spec.choices,
                    "type": (
                        "bool"
                        if spec.non_none_annotation is bool
                        else "int"
                        if spec.non_none_annotation is int
                        else "float"
                        if spec.non_none_annotation is float
                        else "json"
                        if get_origin(spec.non_none_annotation)
                        in {list, dict, tuple}
                        else "str"
                    ),
                    "nullable": spec.is_nullable,
                    "effective_value": effective[name],
                    "source": source,
                    "file_present": name in file_values,
                    "file_value": _json_value(file_values[name])
                    if name in file_values
                    else None,
                    "file_overridden": name in file_values
                    and source in {"cli", "env"},
                }
            )
        from .editing import revision_for

        revision = revision_for(self)
        return {
            "revision": revision,
            "editable": revision is not None and file_error is None,
            "edit_reason": None
            if revision is not None and file_error is None
            else "Read-only configuration",
            "role": self.role,
            "status": "running",
            "startup_version": __version__,
            "startup_at": self.startup_at,
            "config_path": str(self.config_path) if self.config_path else None,
            "config_format": (
                "json"
                if self.config_path and self.config_path.suffix == ".json"
                else "yaml"
            ),
            "file_changed_since_startup": disk_digest
            != self.startup_file_digest,
            "file_error": file_error,
            "fields": fields,
        }


_current: StartupConfigInspection | None = None


def capture_startup_config(role: Role, args: Any, settings: Settings) -> None:
    """Capture one Child's initial CLI/env/file precedence before it serves RPC."""
    global _current
    selected = (
        CONTROL_SETTING_NAMES if role == "control" else EXECUTOR_SETTING_NAMES
    )
    explicit = getattr(args, "config", None)
    configured = explicit or os.environ.get("WORKGATE_CONFIG")
    if configured:
        path: Path | None = Path(configured).expanduser().absolute()
    else:
        default = getattr(args, "_workgate_default_config_path", None)
        path = (
            Path(default).expanduser().absolute()
            if default is not None
            else (
                app_paths().config_file
                if role == "control"
                else app_paths().executor_config_file
            )
        )
        if not path.is_file():
            path = None
    file_names = frozenset(read_config_file(path, setting_names=selected))
    result = StartupConfigInspection(
        role=role,
        settings=settings,
        config_path=path,
        cli_values=cli_overrides_from_args(args),
        env_values=env_overrides(selected),
        startup_file_digest=_file_digest(path),
        startup_file_names=file_names,
        startup_at=time.time(),
    )
    _current = result


def current_startup_config(role: Role) -> StartupConfigInspection | None:
    """Only this role's actual live Child may report its effective settings."""
    if _current is None or _current.role != role:
        return None
    return _current
