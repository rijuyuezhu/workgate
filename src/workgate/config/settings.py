"""Runtime settings for the Workgate workspace control plane."""

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from ..app_paths import app_paths, ensure_private_directory
from ..persistence import StateLayout

AUDIT_LOG_STATE_DIR_NAME = "audit_log"
AUDIT_PAYLOAD_STATE_DIR_NAME = "payloads"
AGENT_AUTH_STATE_DIR_NAME = "agent_auth"
REMOTE_TRANSFER_STATE_DIR_NAME = "remote_transfers"
ENV_PREFIX = "WORKGATE_"
_CONFIG_PATH_FIELDS = frozenset({"workspace_root", "state_dir"})
_RESERVED_UI_PATHS = (
    "/api",
    "/downloads",
    "/healthz",
    "/mcp",
    "/oauth",
    "/openapi.json",
    "/readyz",
    "/redoc",
    "/remote",
    "/docs",
)


def _split_csv(value: str | list[str] | None) -> list[str]:
    """Normalize comma-delimited environment values into list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [x.strip() for x in value.split(",") if x.strip()]


def normalize_ui_path(value: str) -> str:
    """Normalize and validate the browser UI mount path."""
    raw = str(value or "").strip()
    if not raw.startswith("/"):
        raise ValueError("ui_path must start with '/'")
    if any(character in raw for character in ("?", "#", "\\")):
        raise ValueError("ui_path must be a plain URL path")
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError(
            "ui_path must identify a non-root path without dot segments"
        )
    if any(not re.fullmatch(r"[A-Za-z0-9._~-]+", part) for part in parts):
        raise ValueError(
            "ui_path segments may contain only URL-safe ASCII characters"
        )
    normalized = "/" + "/".join(parts)
    for reserved in _RESERVED_UI_PATHS:
        if normalized == reserved or normalized.startswith(reserved + "/"):
            raise ValueError(
                f"ui_path conflicts with reserved service path: {reserved}"
            )
    return normalized


class Settings(BaseSettings):
    """Runtime settings.

    Environment variables use the WORKGATE_ prefix. Optional YAML config can
    be supplied with --config or WORKGATE_CONFIG. Effective precedence is:
    defaults < config file < environment variables < CLI overrides.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, extra="ignore", use_attribute_docstrings=True
    )
    """Pydantic settings configuration for environment loading."""

    # Server.
    mode: Literal["mcp", "http", "both", "stdio"] = "mcp"
    """Server transport mode."""
    host: str = "0.0.0.0"
    """Bind host for HTTP/MCP transports."""
    port: int = 8765
    """Bind port for HTTP/MCP transports."""

    # Human interface.
    ui_enabled: bool = True
    """Mount the browser Human UI and its authenticated API on the HTTP server."""
    ui_path: str = "/ui"
    """Non-root URL path where the browser Human UI is mounted."""
    ui_tui_command: str | None = None
    """Optional administrator-supplied OpenTUI executable command."""
    ui_terminal_idle_timeout_s: int = 3600
    """Idle timeout for authenticated Human UI terminal WebSockets; 0 disables idle expiry."""
    ui_terminal_max_connections: int = 8
    """Maximum concurrent Human UI terminal WebSocket connections."""
    ui_wallpaper: Literal["aurora", "grid", "none"] = "aurora"
    """Browser Human UI background treatment; no external network image is fetched."""

    # Paths and state.
    workspace_root: Path = Field(default_factory=Path.cwd)
    """Workspace filesystem boundary; defaults to the directory Workgate was started from."""
    state_dir: Path = Field(default_factory=lambda: app_paths().state_dir)
    """Directory for durable Workgate runtime state."""

    # Authentication and OAuth.
    auth_mode: Literal["none", "oauth"] = "oauth"
    """Authentication mode. Do not expose public services with none."""
    auth_bypass_localhost: bool = False
    """Allow localhost requests without bearer authentication. Keep disabled when exposing HTTP through proxies or shared hosts."""
    mcp_session_idle_timeout_s: int = 180
    """Idle timeout for stateful Streamable HTTP MCP sessions in seconds."""
    mcp_max_sessions: int = 1024
    """Maximum concurrent stateful Streamable HTTP MCP sessions."""
    base_url: str | None = None
    """Externally reachable base URL used for OAuth metadata, callbacks, and generated links. If unset, URLs fall back to the bind host and port; configure this before exposing the service behind a proxy or public hostname."""
    oauth_issuer: str | None = None
    """Override URL for OAuth issuer metadata; usually derived from base_url."""
    oauth_resource: str | None = None
    """Override URL for OAuth resource metadata; usually derived from base_url plus /mcp."""
    oauth_admin_pin: str | None = None
    """Admin PIN required to approve OAuth authorization. Public OAuth URLs require a non-placeholder value of at least 8 characters."""
    oauth_access_token_ttl_s: int = 3600
    """Bearer token lifetime in seconds. After this time, the token must be re-authorized and refreshed."""
    oauth_code_ttl_s: int = 300
    """OAuth authorization-code lifetime in seconds. The authorization must be done within this time."""
    oauth_max_pending_codes: int = 2048
    """Maximum unused, unexpired OAuth authorization codes kept in memory; set to 0 to disable this capacity limit."""
    oauth_client_ttl_s: int = 86400
    """Pending OAuth client registration lifetime in seconds. Approved clients are persisted without this TTL; set to 0 to disable pending expiration."""
    oauth_max_dynamic_clients: int = 256
    """Maximum pending OAuth client registrations kept in memory. Approved clients do not count; set to 0 to disable this capacity limit."""
    oauth_registration_max_body_bytes: int = 16384
    """Maximum JSON body size accepted by dynamic OAuth client registration."""
    oauth_registration_max_redirect_uris: int = 10
    """Maximum redirect URIs accepted in one dynamic OAuth client registration."""
    oauth_registration_max_redirect_uri_chars: int = 2048
    """Maximum length of each dynamic OAuth client redirect URI."""
    oauth_registration_max_client_name_chars: int = 200
    """Maximum length of a dynamic OAuth client display name."""

    # Safety and resource limits.
    allow_full_control: bool = False
    """Disable built-in workspace and command restrictions; use only in disposable containers or VMs. MCP safety annotations remain conservative in this mode."""
    """Allow network-capable operations."""
    tool_timeout_s: float = 60
    """Base MCP/HTTP tool watchdog timeout in seconds. Shell execution tools receive additional bounded cleanup time beyond run_shell_max_timeout_s."""
    run_shell_default_timeout_s: int = 10
    """Default timeout for bounded shell command calls in seconds."""
    run_shell_max_timeout_s: int = 120
    """Maximum timeout accepted by bounded shell command calls in seconds."""
    max_output_bytes: int = 200_000
    """Command output limit in bytes."""
    max_job_log_bytes: int = 10_000_000
    """Maximum retained output bytes for one tracked background-job attempt."""
    max_jobs: int = 1_000
    """Maximum retained tracked-job records; active jobs are never pruned."""
    max_agent_sessions: int = Field(default=256, ge=1, le=10_000)
    """Maximum durable agent/workspace sessions after stale-session pruning."""
    agent_session_retention_s: int = Field(
        default=30 * 24 * 60 * 60,
        ge=0,
        le=366 * 24 * 60 * 60,
    )
    """Idle retention for durable agent/workspace sessions; 0 disables age-based expiry."""
    max_session_snapshots: int = Field(default=2_000, ge=1, le=100_000)
    """Maximum grounding snapshots retained for one agent session."""
    max_session_snapshot_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1_024,
        le=16_000_000,
    )
    """Maximum encoded grounding-snapshot metadata retained for one session."""
    max_file_read_bytes: int = 512_000
    """Per-file read limit in bytes."""
    max_view_image_bytes: int = 20 * 1024 * 1024
    """Maximum raw bytes accepted by the native MCP image viewer."""
    max_skills: int = 256
    """Maximum number of discovered Skills across all configured sources."""
    max_skill_related_files: int = 1_000
    """Maximum related files returned for one Skill."""
    max_skill_scan_entries: int = 5_000
    """Maximum filesystem entries inspected during one Skill registry scan."""
    max_skill_path_bytes: int = 200_000
    """Maximum UTF-8 bytes used by returned related Skill paths."""
    max_file_write_bytes: int = 5_000_000
    """Per-file write/edit limit in bytes."""
    max_grep_results: int = 200
    """Maximum grep result count."""
    max_directory_entries: int = 5_000
    """Maximum listed directory entries."""
    max_glob_results: int = 5_000
    """Maximum glob search results."""
    max_tree_entries: int = 5_000
    """Maximum tree-view entries."""
    max_todos: int = 1_000
    """Todo-list item limit."""
    max_todo_bytes: int = 1_000_000
    """Todo-list total byte limit."""
    max_http_request_bytes: int = 16_000_000
    """Maximum inbound HTTP request-body bytes; 0 disables the shared limit."""
    max_audit_log_bytes: int = 20_000_000
    """Maximum active audit JSONL bytes before atomic recent-record retention."""
    max_audit_event_bytes: int = 1_000_000
    """Maximum encoded bytes retained for one audit event before preview truncation."""
    audit_payloads_enabled: bool = True
    """Store large sanitized audit field values as private content-addressed payloads."""
    audit_inline_value_bytes: int = Field(
        default=16 * 1024, ge=256, le=16_000_000
    )
    """Maximum canonical JSON bytes kept inline for one sanitized audit field value."""
    max_audit_payload_bytes: int = Field(
        default=64 * 1024 * 1024, ge=1_024, le=1_000_000_000
    )
    """Maximum canonical JSON bytes accepted for one recoverable sanitized audit payload."""
    max_audit_payload_store_bytes: int = Field(
        default=256 * 1024 * 1024, ge=1_024, le=4_000_000_000
    )
    """Maximum compressed bytes retained in the private audit payload store."""
    audit_payload_retention_s: int = Field(
        default=7 * 24 * 60 * 60, ge=0, le=366 * 24 * 60 * 60
    )
    """Recovery lifetime and orphan grace period for private audit payload objects."""
    max_tmp_files: int = 500
    """Temporary-file count limit. When exceeded, old files are deleted."""
    max_tmp_bytes: int = 50_000_000
    """Temporary-file byte limit. When exceeded, old files are deleted."""
    max_transfer_archive_entries: int = 100_000
    """Maximum entries accepted from one transferred archive."""
    max_transfer_unpacked_bytes: int = 10_000_000_000
    """Maximum declared regular-file bytes accepted while unpacking an archive."""
    max_concurrent_commands: int = 4
    """Concurrent command limit."""
    max_tmux_sessions: int = 16
    """Persistent shell limit."""
    file_download_enabled: bool = True
    """Enable download links created by protected tools."""
    file_download_default_ttl_s: int = 3600
    """Default lifetime for file download links in seconds."""
    file_download_max_ttl_s: int = 604800
    """Maximum lifetime accepted for file download links in seconds."""
    file_download_default_max_downloads: int = 0
    """Default download-count limit for file links; 0 means unlimited until expiry."""
    file_download_max_file_bytes: int = 0
    """Maximum file size allowed for download links; 0 disables this size limit."""
    command_denylist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "docker.sock",
            "/var/run/docker.sock",
            "mkfs",
            "mount",
            "umount",
            "shutdown",
            "reboot",
            "systemctl",
            "iptables",
            "nft",
        ]
    )
    """Comma-separated command denylist in env/CLI, or a YAML list in config files. Cleared when full-control mode is enabled."""
    path_denylist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            ".ssh/id_rsa",
            ".ssh/id_ed25519",
            ".env",
            "secrets",
            "credentials",
            ".git/config",
        ]
    )
    """Comma-separated path denylist in env/CLI, or a YAML list in config files. Cleared when full-control mode is enabled."""

    # Executor transport.
    executor_max_pending_commands: int = Field(default=64, ge=1)
    """Maximum queued or offered ordinary commands retained per executor."""

    # Remote workers.
    remote_enabled: bool = True
    """Enable remote worker routes and MCP tools."""
    remote_invite_ttl_s: int = 600
    """One-time remote worker invite lifetime in seconds."""
    remote_poll_timeout_s: int = 25
    """Remote worker long-poll heartbeat timeout in seconds."""
    remote_job_timeout_s: int = 3600
    """Control-side remote job result timeout in seconds."""
    remote_max_pending_jobs: int = 64
    """Maximum queued or in-flight remote jobs allowed per worker."""
    remote_http_transfer_enabled: bool = True
    """Use the private resumable HTTP gateway for capable large session copies."""
    remote_http_transfer_threshold_bytes: int = 1024 * 1024
    """Minimum payload size selected for private HTTP streaming."""
    remote_http_transfer_chunk_bytes: int = 1024 * 1024
    """Maximum request or response chunk size for private HTTP transfer routes."""
    remote_http_transfer_ticket_ttl_s: int = 300
    """Lifetime of a private transfer grant before it must be renewed."""
    remote_http_transfer_max_active: int = 16
    """Maximum non-terminal private transfer objects retained concurrently."""
    remote_http_transfer_max_spool_bytes: int = 10 * 1024 * 1024 * 1024
    """Maximum aggregate bytes reserved by private controller transfer spools."""

    # Agent capability bridge.
    agent_bridge_enabled: bool = True
    """Enable agent capability bridge tools."""
    agent_mcp_probe_timeout_s: int = 5
    """Agent MCP server probe timeout in seconds."""
    agent_mcp_call_timeout_s: int = 60
    """Agent MCP tool-call timeout in seconds."""
    agent_dynamic_mcp_tools: bool = True
    """Register dynamic MCP bridge tools."""
    agent_dynamic_skill_tools: bool = True
    """Register dynamic skill bridge tools."""

    # Tool executables.
    shell_executable: str = "/bin/bash"
    """Shell executable for bounded and persistent sessions; the POSIX default maps to COMSPEC on Windows."""
    tmux_bin: str = "tmux"
    """tmux executable required for persistent shells on POSIX systems."""
    rg_bin: str = "rg"
    """ripgrep executable."""
    git_bin: str = "git"
    """Git executable used for patch validation and application."""
    python_bin: str = "python3"
    """Python executable; the POSIX default maps to the running interpreter on Windows."""

    @property
    def audit_log_path(self) -> Path:
        """Path to the JSONL audit log, derived from state_dir."""
        return StateLayout(self.state_dir).audit_log_path

    @property
    def audit_payload_dir(self) -> Path:
        """Private content-addressed audit payload directory."""
        return StateLayout(self.state_dir).audit_payload_dir

    @property
    def agent_config_dir(self) -> Path:
        """Declarative Agent Bridge configuration directory."""
        return app_paths().agent_config_dir

    @property
    def agent_auth_dir(self) -> Path:
        """Private Agent Bridge credential directory, derived from state_dir."""
        return StateLayout(self.state_dir).agent_auth_dir

    @property
    def remote_transfer_dir(self) -> Path:
        """Private durable ticket and spool directory for HTTP transfers."""
        return StateLayout(self.state_dir).remote_transfers_dir

    @property
    def config_dir(self) -> Path:
        """Platform-native Workgate configuration namespace."""
        return app_paths().config_dir

    @property
    def data_dir(self) -> Path:
        """Platform-native Workgate persistent-data namespace."""
        return app_paths().data_dir

    @property
    def cache_dir(self) -> Path:
        """Platform-native Workgate regenerable-cache namespace."""
        return app_paths().cache_dir

    @property
    def runtime_dir(self) -> Path:
        """Private safely disposable Workgate runtime namespace."""
        return app_paths().runtime_dir

    @property
    def resolved_base_url(self) -> str:
        """Configured base_url, or a local HTTP URL derived from host and port."""
        if self.base_url:
            return self.base_url.rstrip("/")
        host = self.host
        if host in {"", "0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @field_validator(
        "workspace_root",
        "state_dir",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value: str | Path) -> Path:
        """Expand user and environment variables for path settings before validation."""
        expanded = os.path.expandvars(os.path.expanduser(str(value)))
        return Path(os.path.abspath(expanded))

    @field_validator("ui_path", mode="before")
    @classmethod
    def validate_ui_path(cls, value: str) -> str:
        """Reject root, traversal, and service-reserved Human UI paths."""
        return normalize_ui_path(value)

    @field_validator("ui_terminal_idle_timeout_s")
    @classmethod
    def validate_ui_terminal_idle_timeout(cls, value: int) -> int:
        """Reject negative Human UI terminal idle timeouts."""
        if value < 0:
            raise ValueError("ui_terminal_idle_timeout_s must be non-negative")
        return value

    @field_validator("ui_terminal_max_connections")
    @classmethod
    def validate_ui_terminal_max_connections(cls, value: int) -> int:
        """Bound concurrent Human UI terminal WebSocket connections."""
        if not 1 <= value <= 128:
            raise ValueError(
                "ui_terminal_max_connections must be between 1 and 128"
            )
        return value

    @field_validator("command_denylist", "path_denylist", mode="before")
    @classmethod
    def split_csv_fields(cls, value: str | list[str] | None) -> list[str]:
        """Normalize comma-delimited restriction lists supplied through environment variables."""
        return _split_csv(value)

    @model_validator(mode="after")
    def validate_remote_transfer_limits(self) -> Settings:
        """Keep private HTTP transfer limits positive and internally consistent."""
        positive = {
            "remote_http_transfer_threshold_bytes": self.remote_http_transfer_threshold_bytes,
            "remote_http_transfer_chunk_bytes": self.remote_http_transfer_chunk_bytes,
            "remote_http_transfer_ticket_ttl_s": self.remote_http_transfer_ticket_ttl_s,
            "remote_http_transfer_max_active": self.remote_http_transfer_max_active,
            "remote_http_transfer_max_spool_bytes": self.remote_http_transfer_max_spool_bytes,
        }
        for name, value in positive.items():
            if int(value) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.remote_http_transfer_chunk_bytes > 4 * 1024 * 1024:
            raise ValueError(
                "remote_http_transfer_chunk_bytes must not exceed 4194304"
            )
        if (
            self.remote_http_transfer_threshold_bytes
            > self.remote_http_transfer_max_spool_bytes
        ):
            raise ValueError(
                "remote_http_transfer_threshold_bytes must not exceed "
                "remote_http_transfer_max_spool_bytes"
            )
        return self

    @model_validator(mode="after")
    def validate_audit_payload_limits(self) -> Settings:
        """Keep nested audit payload limits internally consistent."""
        if self.audit_inline_value_bytes > self.max_audit_payload_bytes:
            raise ValueError(
                "audit_inline_value_bytes must not exceed max_audit_payload_bytes"
            )
        if self.max_audit_payload_bytes > self.max_audit_payload_store_bytes:
            raise ValueError(
                "max_audit_payload_bytes must not exceed max_audit_payload_store_bytes"
            )
        return self

    @model_validator(mode="after")
    def disable_builtin_restrictions_in_full_container_mode(self) -> Settings:
        """Remove built-in command and path restrictions when full-control mode is explicitly enabled."""
        if self.allow_full_control:
            self.command_denylist = []
            self.path_denylist = []
        return self


def read_config_file(path: str | Path | None) -> dict[str, Any]:
    """Read optional YAML configuration values."""
    if not path:
        return {}
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(config_path)
    loaded = yaml.safe_load(config_path.read_text())
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a mapping: {config_path}")
    for name in _CONFIG_PATH_FIELDS.intersection(loaded):
        value = loaded[name]
        if value is None:
            continue
        expanded = os.path.expandvars(os.path.expanduser(str(value)))
        if not Path(expanded).is_absolute():
            raise ValueError(
                f"Config setting {name} must be an absolute path after "
                f"user/environment expansion: {value!r}"
            )
    return loaded


def env_overrides() -> dict[str, Any]:
    """Return settings explicitly present in the process environment."""
    present = {
        name: field_name
        for field_name in Settings.model_fields
        if (name := f"{ENV_PREFIX}{field_name.upper()}") in os.environ
    }
    if not present:
        return {}
    env_settings = Settings()
    return {
        field_name: getattr(env_settings, field_name)
        for field_name in present.values()
    }


def initialize_runtime_directories(settings: Settings) -> None:
    """Create the filesystem roots required by a configured runtime."""
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    ensure_private_directory(settings.state_dir)
    ensure_private_directory(settings.audit_log_path.parent)


def load_settings(
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load settings without mutating the runtime filesystem."""
    selected_config: str | Path | None = config_path
    if selected_config is None:
        selected_config = os.getenv("WORKGATE_CONFIG")
    if (
        selected_config is None
        and os.getenv("WORKGATE_REMOTE_WORKER_RUNTIME") != "1"
    ):
        default_config = app_paths().config_file
        selected_config = default_config if default_config.is_file() else None
    values = read_config_file(selected_config)
    values.update(env_overrides())
    if overrides:
        values.update(overrides)
    return Settings(**values)


_configured_settings: Settings | None = None


def get_settings() -> Settings:
    """Return cached settings, optionally primed by configure_settings. If no settings are cached, a new one is loaded from load_settings without any CLI overrides."""
    global _configured_settings
    if _configured_settings is None:
        _configured_settings = load_settings()
    return _configured_settings


def configure_settings(settings: Settings) -> None:
    """Install a fully resolved Settings object for subsequent get_settings calls."""
    global _configured_settings
    _configured_settings = settings


def clear_settings_cache() -> None:
    """Clear cached settings. Intended for tests and CLI reconfiguration."""
    global _configured_settings
    _configured_settings = None
