"""Resolved control-plane configuration view."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config.settings import Settings


@dataclass(frozen=True, slots=True)
class ControlConfig:
    """Resolved configuration containing only control-owned authority."""

    mode: Literal["mcp", "http", "both", "stdio"]
    host: str
    port: int
    state_dir: Path
    base_url: str | None
    resolved_base_url: str

    auth_mode: Literal["none", "oauth"]
    oauth_admin_pin: str | None
    oauth_access_token_ttl_s: int
    mcp_session_idle_timeout_s: int
    mcp_max_sessions: int

    ui_enabled: bool
    ui_path: str
    ui_tui_command: str | None
    ui_terminal_idle_timeout_s: int
    ui_wallpaper: Literal["aurora", "grid", "none"]

    executor_max_pending_commands: int
    executor_pairing_max_pending: int
    executor_pairing_ttl_s: int
    max_agent_sessions: int
    agent_session_retention_s: int

    tool_timeout_s: float
    max_http_request_bytes: int
    max_todos: int
    max_todo_bytes: int

    file_download_enabled: bool
    file_download_default_ttl_s: int
    file_download_max_ttl_s: int
    file_download_default_max_downloads: int
    file_download_max_file_bytes: int

    agent_bridge_enabled: bool
    agent_config_dir: Path
    agent_auth_dir: Path
    agent_mcp_probe_timeout_s: int
    agent_mcp_call_timeout_s: int
    agent_dynamic_mcp_tools: bool


def resolve_control_config(settings: Settings) -> ControlConfig:
    """Snapshot only control-owned authority from the user-facing settings."""
    return ControlConfig(
        mode=settings.mode,
        host=settings.host,
        port=settings.port,
        state_dir=settings.state_dir.resolve(strict=False),
        base_url=settings.base_url,
        resolved_base_url=settings.resolved_base_url,
        auth_mode=settings.auth_mode,
        oauth_admin_pin=settings.oauth_admin_pin,
        oauth_access_token_ttl_s=settings.oauth_access_token_ttl_s,
        mcp_session_idle_timeout_s=settings.mcp_session_idle_timeout_s,
        mcp_max_sessions=settings.mcp_max_sessions,
        ui_enabled=settings.ui_enabled,
        ui_path=settings.ui_path,
        ui_tui_command=settings.ui_tui_command,
        ui_terminal_idle_timeout_s=settings.ui_terminal_idle_timeout_s,
        ui_wallpaper=settings.ui_wallpaper,
        executor_max_pending_commands=settings.executor_max_pending_commands,
        executor_pairing_max_pending=settings.executor_pairing_max_pending,
        executor_pairing_ttl_s=settings.executor_pairing_ttl_s,
        max_agent_sessions=settings.max_agent_sessions,
        agent_session_retention_s=settings.agent_session_retention_s,
        tool_timeout_s=settings.tool_timeout_s,
        max_http_request_bytes=settings.max_http_request_bytes,
        max_todos=settings.max_todos,
        max_todo_bytes=settings.max_todo_bytes,
        file_download_enabled=settings.file_download_enabled,
        file_download_default_ttl_s=settings.file_download_default_ttl_s,
        file_download_max_ttl_s=settings.file_download_max_ttl_s,
        file_download_default_max_downloads=settings.file_download_default_max_downloads,
        file_download_max_file_bytes=settings.file_download_max_file_bytes,
        agent_bridge_enabled=settings.agent_bridge_enabled,
        agent_config_dir=settings.agent_config_dir.resolve(strict=False),
        agent_auth_dir=settings.agent_auth_dir.resolve(strict=False),
        agent_mcp_probe_timeout_s=settings.agent_mcp_probe_timeout_s,
        agent_mcp_call_timeout_s=settings.agent_mcp_call_timeout_s,
        agent_dynamic_mcp_tools=settings.agent_dynamic_mcp_tools,
    )
