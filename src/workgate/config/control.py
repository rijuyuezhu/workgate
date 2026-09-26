"""Resolved control-plane configuration authority."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .role_config import SharedRoleConfig, current_role_config
from .settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class ControlConfig(SharedRoleConfig):
    """Resolved configuration containing only control-owned authority."""

    mode: Literal["mcp", "http", "both", "stdio"]
    host: str
    port: int
    data_dir: Path
    base_url: str | None
    resolved_base_url: str

    auth_mode: Literal["none", "oauth"]
    auth_bypass_localhost: bool
    oauth_issuer: str | None
    oauth_resource: str | None
    oauth_admin_pin: str | None
    oauth_access_token_ttl_s: int
    oauth_code_ttl_s: int
    oauth_max_pending_codes: int
    oauth_client_ttl_s: int
    oauth_max_dynamic_clients: int
    oauth_registration_max_body_bytes: int
    oauth_registration_max_redirect_uris: int
    oauth_registration_max_redirect_uri_chars: int
    oauth_registration_max_client_name_chars: int
    mcp_session_idle_timeout_s: int
    mcp_max_sessions: int

    ui_enabled: bool
    ui_path: str
    ui_tui_command: str | None
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
    agent_dynamic_mcp_tools: bool


CONTROL_SETTING_NAMES = frozenset(
    ControlConfig.__dataclass_fields__
) & frozenset(Settings.model_fields)


def get_control_config() -> ControlConfig:
    """Return the control config bound to the current execution context."""
    config = current_role_config()
    if config is None:
        return resolve_control_config(get_settings())
    if not isinstance(config, ControlConfig):
        raise RuntimeError("current execution context is not control-owned")
    return config


def resolve_control_config(settings: Settings) -> ControlConfig:
    """Snapshot only control-owned authority from the user-facing settings."""
    return ControlConfig(
        mode=settings.mode,
        host=settings.host,
        port=settings.port,
        state_dir=settings.state_dir.resolve(strict=False),
        data_dir=settings.data_dir.resolve(strict=False),
        base_url=settings.base_url,
        resolved_base_url=settings.resolved_base_url,
        auth_mode=settings.auth_mode,
        auth_bypass_localhost=settings.auth_bypass_localhost,
        oauth_issuer=settings.oauth_issuer,
        oauth_resource=settings.oauth_resource,
        oauth_admin_pin=settings.oauth_admin_pin,
        oauth_access_token_ttl_s=settings.oauth_access_token_ttl_s,
        oauth_code_ttl_s=settings.oauth_code_ttl_s,
        oauth_max_pending_codes=settings.oauth_max_pending_codes,
        oauth_client_ttl_s=settings.oauth_client_ttl_s,
        oauth_max_dynamic_clients=settings.oauth_max_dynamic_clients,
        oauth_registration_max_body_bytes=settings.oauth_registration_max_body_bytes,
        oauth_registration_max_redirect_uris=settings.oauth_registration_max_redirect_uris,
        oauth_registration_max_redirect_uri_chars=settings.oauth_registration_max_redirect_uri_chars,
        oauth_registration_max_client_name_chars=settings.oauth_registration_max_client_name_chars,
        mcp_session_idle_timeout_s=settings.mcp_session_idle_timeout_s,
        mcp_max_sessions=settings.mcp_max_sessions,
        ui_enabled=settings.ui_enabled,
        ui_path=settings.ui_path,
        ui_tui_command=settings.ui_tui_command,
        ui_terminal_idle_timeout_s=settings.ui_terminal_idle_timeout_s,
        ui_terminal_max_connections=settings.ui_terminal_max_connections,
        ui_wallpaper=settings.ui_wallpaper,
        executor_max_pending_commands=settings.executor_max_pending_commands,
        executor_pairing_max_pending=settings.executor_pairing_max_pending,
        executor_pairing_ttl_s=settings.executor_pairing_ttl_s,
        max_agent_sessions=settings.max_agent_sessions,
        agent_session_retention_s=settings.agent_session_retention_s,
        tool_timeout_s=settings.tool_timeout_s,
        max_job_log_bytes=settings.max_job_log_bytes,
        max_jobs=settings.max_jobs,
        max_view_image_bytes=settings.max_view_image_bytes,
        max_http_request_bytes=settings.max_http_request_bytes,
        max_audit_log_bytes=settings.max_audit_log_bytes,
        max_audit_event_bytes=settings.max_audit_event_bytes,
        audit_payloads_enabled=settings.audit_payloads_enabled,
        audit_inline_value_bytes=settings.audit_inline_value_bytes,
        max_audit_payload_bytes=settings.max_audit_payload_bytes,
        max_audit_payload_store_bytes=settings.max_audit_payload_store_bytes,
        audit_payload_retention_s=settings.audit_payload_retention_s,
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
