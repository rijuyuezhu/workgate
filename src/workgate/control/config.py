"""Resolved control-plane configuration view."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config.settings import Settings


@dataclass(frozen=True, slots=True)
class ControlConfig:
    """Control-owned settings needed by new control-plane composition code."""

    mode: Literal["mcp", "http", "both", "stdio"]
    host: str
    port: int
    state_dir: Path
    resolved_base_url: str
    auth_mode: Literal["none", "oauth"]
    ui_enabled: bool
    executor_max_pending_commands: int

    remote_enabled: bool
    remote_max_pending_jobs: int
    max_agent_sessions: int
    agent_session_retention_s: int


def resolve_control_config(settings: Settings) -> ControlConfig:
    """Snapshot control-owned authority from the legacy monolithic settings."""
    return ControlConfig(
        mode=settings.mode,
        host=settings.host,
        port=settings.port,
        state_dir=settings.state_dir.resolve(strict=False),
        resolved_base_url=settings.resolved_base_url,
        auth_mode=settings.auth_mode,
        ui_enabled=settings.ui_enabled,
        executor_max_pending_commands=settings.executor_max_pending_commands,
        remote_enabled=settings.remote_enabled,
        remote_max_pending_jobs=settings.remote_max_pending_jobs,
        max_agent_sessions=settings.max_agent_sessions,
        agent_session_retention_s=settings.agent_session_retention_s,
    )
