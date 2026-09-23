"""Resolve one user-facing standalone configuration into distinct child configs."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from ..config.roles import (
    CONTROL_EXCLUDED_SETTING_NAMES,
    EXECUTOR_SETTING_NAMES,
)
from ..config.settings import Settings

_FORCED_CONTROL_VALUES: dict[str, object] = {
    "mode": "mcp",
    "host": "127.0.0.1",
    "base_url": None,
    "auth_mode": "oauth",
    "auth_bypass_localhost": False,
    "oauth_issuer": None,
    "oauth_resource": None,
}


@dataclass(frozen=True, slots=True)
class StandaloneChildConfig:
    """Fully resolved role-specific configuration for standalone children."""

    control: dict[str, object]
    executor: dict[str, object]
    control_state_dir: Path
    executor_state_dir: Path
    control_data_dir: Path
    control_url: str
    instance_namespace: str


def _model_payload(settings: Settings) -> dict[str, object]:
    return dict(settings.model_dump(mode="json"))


def standalone_instance_namespace(state_root: Path) -> str:
    """Return a stable short namespace for one standalone state root."""
    resolved = state_root.resolve(strict=False)
    canonical = os.path.normcase(str(resolved))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _control_setting_names() -> frozenset[str]:
    return frozenset(Settings.model_fields) - CONTROL_EXCLUDED_SETTING_NAMES


def resolve_standalone_child_config(
    settings: Settings,
) -> StandaloneChildConfig:
    """Split one resolved user config into separate control/executor authority."""
    if not 1 <= settings.port <= 65535:
        raise ValueError(
            "standalone requires an explicit TCP port between 1 and 65535"
        )
    payload = _model_payload(settings)
    standalone_state = settings.state_dir.resolve(strict=False) / "standalone"
    instance_namespace = standalone_instance_namespace(standalone_state)
    standalone_data = (
        settings.data_dir.resolve(strict=False)
        / "standalone"
        / instance_namespace
    )
    control_state = standalone_state / "control"
    executor_state = standalone_state / "executor"
    control_data = standalone_data / "control"

    control = {
        name: payload[name]
        for name in _control_setting_names()
        if name in payload
    }
    control.update(_FORCED_CONTROL_VALUES)
    control["state_dir"] = str(control_state)
    control["data_dir"] = str(control_data)

    executor = {
        name: payload[name]
        for name in EXECUTOR_SETTING_NAMES
        if name in payload
    }
    executor["state_dir"] = str(executor_state)
    executor["workspace_root"] = str(
        settings.workspace_root.resolve(strict=False)
    )

    return StandaloneChildConfig(
        control=control,
        executor=executor,
        control_state_dir=control_state,
        executor_state_dir=executor_state,
        control_data_dir=control_data,
        control_url=f"http://127.0.0.1:{settings.port}",
        instance_namespace=instance_namespace,
    )
