"""Runtime settings for Workgate."""

import os
import re
from collections.abc import Collection, Mapping
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
ENV_PREFIX = "WORKGATE_"
_CONFIG_PATH_FIELDS = frozenset({"default_workdir", "state_dir", "data_dir"})
_RESERVED_UI_PATHS = (
    "/api",
    "/downloads",
    "/healthz",
    "/mcp",
    "/oauth",
    "/openapi.json",
    "/readyz",
    "/redoc",
    "/docs",
)

_PositiveInt = Annotated[int, Field(gt=0)]
_NonNegativeInt = Annotated[int, Field(ge=0)]
_PositiveFloat = Annotated[float, Field(gt=0)]
_Port = Annotated[int, Field(ge=1, le=65535)]
_StringList = Annotated[list[str], NoDecode]


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
    host: str = "127.0.0.1"
    """Bind host for HTTP/MCP transports; defaults to loopback for reverse-proxy deployments."""
    port: _Port = 8765
    """Bind port for HTTP/MCP transports."""
    log_level: Literal["critical", "error", "warning", "info", "debug"] = (
        "warning"
    )
    """Runtime log level for Workgate and its HTTP server."""
    forwarded_allow_ips: str = "127.0.0.1"
    """Comma-separated proxy addresses/networks trusted for forwarded HTTP headers."""

    # Human interface.
    ui_enabled: bool = True
    """Mount the browser Human UI and its authenticated API on the HTTP server."""
    ui_path: str = "/ui"
    """Non-root URL path where the browser Human UI is mounted."""
    ui_tui_command: str | None = None
    """Optional administrator-supplied OpenTUI executable command."""
    ui_terminal_idle_timeout_s: _NonNegativeInt = 3600
    """Idle timeout for authenticated Human UI terminal WebSockets; 0 disables idle expiry."""
    ui_terminal_max_connections: int = Field(default=8, ge=1, le=128)
    """Maximum concurrent Human UI terminal WebSocket connections."""
    ui_wallpaper: Literal["aurora", "grid", "none"] = "aurora"
    """Browser Human UI background treatment; no external network image is fetched."""

    # Paths and state.
    default_workdir: Path = Field(default_factory=Path.cwd)
    """Default executor working directory; missing defaults fall back to the filesystem root."""
    state_dir: Path = Field(default_factory=lambda: app_paths().state_dir)
    """Directory for durable Workgate runtime state."""
    data_dir: Path = Field(default_factory=lambda: app_paths().data_dir)
    """Directory for durable Workgate application data such as immutable control payloads."""

    # Authentication and OAuth.
    auth_mode: Literal["none", "oauth"] = "oauth"
    """Authentication mode. Do not expose public services with none."""
    auth_bypass_localhost: bool = False
    """Allow direct loopback HTTP requests without bearer authentication. Forwarded or non-loopback Host requests still require OAuth."""
    mcp_session_idle_timeout_s: _PositiveInt = 180
    """Idle timeout for stateful Streamable HTTP MCP sessions in seconds."""
    mcp_max_sessions: _PositiveInt = 1024
    """Maximum concurrent stateful Streamable HTTP MCP sessions."""
    base_url: str | None = None
    """Externally reachable base URL used for OAuth metadata, callbacks, and generated links. If unset, URLs fall back to the bind host and port; configure this before exposing the service behind a proxy or public hostname."""
    oauth_issuer: str | None = None
    """Override URL for OAuth issuer metadata; usually derived from base_url."""
    oauth_resource: str | None = None
    """Override URL for OAuth resource metadata; usually derived from base_url plus /mcp."""
    oauth_admin_pin: str | None = None
    """Admin PIN required to approve OAuth authorization. Public OAuth URLs require a non-placeholder value of at least 8 characters."""
    oauth_access_token_ttl_s: _NonNegativeInt = 3600
    """Bearer token lifetime in seconds; 0 disables token expiry."""
    oauth_code_ttl_s: _PositiveInt = 300
    """OAuth authorization-code lifetime in seconds. The authorization must be done within this time."""
    oauth_max_pending_codes: _NonNegativeInt = 2048
    """Maximum unused, unexpired OAuth authorization codes kept in memory; set to 0 to disable this capacity limit."""
    oauth_client_ttl_s: _NonNegativeInt = 86400
    """Pending OAuth client registration lifetime in seconds. Approved clients are persisted without this TTL; set to 0 to disable pending expiration."""
    oauth_max_dynamic_clients: _NonNegativeInt = 256
    """Maximum pending OAuth client registrations kept in memory. Approved clients do not count; set to 0 to disable this capacity limit."""
    oauth_registration_max_body_bytes: _PositiveInt = 16384
    """Maximum JSON body size accepted by dynamic OAuth client registration."""
    oauth_registration_max_redirect_uris: _PositiveInt = 10
    """Maximum redirect URIs accepted in one dynamic OAuth client registration."""
    oauth_registration_max_redirect_uri_chars: _PositiveInt = 2048
    """Maximum length of each dynamic OAuth client redirect URI."""
    oauth_registration_max_client_name_chars: _PositiveInt = 200
    """Maximum length of a dynamic OAuth client display name."""

    # Safety and resource limits.
    tool_timeout_s: _PositiveFloat = 60
    """Base control-owned MCP/HTTP tool watchdog timeout in seconds."""
    run_shell_default_timeout_s: _PositiveInt = 10
    """Default timeout for bounded shell command calls in seconds."""
    run_shell_max_timeout_s: _PositiveInt = 120
    """Maximum timeout accepted by bounded shell command calls in seconds."""
    subprocess_env_blocklist: _StringList = Field(default_factory=list)
    """Additional exact environment variable names withheld from user-launched subprocesses."""
    subprocess_env_blocked_prefixes: _StringList = Field(default_factory=list)
    """Additional environment variable prefixes withheld from user-launched subprocesses."""
    max_output_bytes: _PositiveInt = 200_000
    """Command output limit in bytes."""
    max_job_log_bytes: _PositiveInt = 10_000_000
    """Maximum retained output bytes for one tracked background-job attempt."""
    max_jobs: _NonNegativeInt = 1_000
    """Maximum retained tracked-job records; 0 keeps only active jobs."""
    max_agent_sessions: int = Field(default=256, ge=1, le=10_000)
    """Maximum durable execution sessions after stale-session pruning."""
    agent_session_retention_s: int = Field(
        default=30 * 24 * 60 * 60,
        ge=0,
        le=366 * 24 * 60 * 60,
    )
    """Idle retention for durable execution sessions; 0 disables age-based expiry."""
    max_session_snapshots: int = Field(default=2_000, ge=1, le=100_000)
    """Maximum grounding snapshots retained for one execution session."""
    max_session_snapshot_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1_024,
        le=16_000_000,
    )
    """Maximum encoded grounding-snapshot metadata retained for one session."""
    max_file_read_bytes: _PositiveInt = 512_000
    """Per-file read limit in bytes."""
    max_view_image_bytes: _PositiveInt = 20 * 1024 * 1024
    """Maximum raw bytes accepted by the native MCP image viewer."""
    max_skills: _PositiveInt = 256
    """Maximum number of discovered Skills across all configured sources."""
    max_skill_related_files: _PositiveInt = 1_000
    """Maximum related files returned for one Skill."""
    max_skill_scan_entries: _PositiveInt = 5_000
    """Maximum filesystem entries inspected during one Skill registry scan."""
    max_skill_path_bytes: _PositiveInt = 200_000
    """Maximum UTF-8 bytes used by returned related Skill paths."""
    max_file_write_bytes: _PositiveInt = 5_000_000
    """Per-file write/edit limit in bytes."""
    max_grep_results: _PositiveInt = 200
    """Maximum grep result count."""
    max_directory_entries: _PositiveInt = 5_000
    """Maximum listed directory entries."""
    max_glob_results: _PositiveInt = 5_000
    """Maximum glob search results."""
    max_tree_entries: _PositiveInt = 5_000
    """Maximum tree-view entries."""
    max_todos: _PositiveInt = 1_000
    """Todo-list item limit."""
    max_todo_bytes: _PositiveInt = 1_000_000
    """Todo-list total byte limit."""
    max_http_request_bytes: _NonNegativeInt = 16_000_000
    """Maximum inbound HTTP request-body bytes; 0 disables the shared limit."""
    max_audit_log_bytes: _NonNegativeInt = 20_000_000
    """Maximum active audit JSONL bytes before recent-record retention; 0 disables this size cap."""
    max_audit_event_bytes: _PositiveInt = 1_000_000
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
    max_tmp_files: _NonNegativeInt = 500
    """Temporary-file count limit; 0 removes all eligible scratch files during pruning."""
    max_tmp_bytes: _NonNegativeInt = 50_000_000
    """Temporary-file byte limit; 0 removes all eligible scratch files during pruning."""
    max_transfer_payload_bytes: _PositiveInt = 4 * 1024 * 1024 * 1024
    """Maximum retained bytes admitted for one cross-executor transfer payload."""
    max_transfer_payload_store_bytes: _PositiveInt = 16 * 1024 * 1024 * 1024
    """Maximum aggregate bytes reserved or retained for cross-executor transfer payloads."""
    max_transfer_archive_entries: _PositiveInt = 100_000
    """Maximum entries accepted from one transferred archive."""
    max_transfer_unpacked_bytes: _PositiveInt = 10_000_000_000
    """Maximum declared regular-file bytes accepted while unpacking an archive."""
    max_concurrent_commands: _PositiveInt = 4
    """Concurrent command limit."""
    max_tmux_sessions: _PositiveInt = 16
    """Persistent shell limit."""
    file_download_enabled: bool = True
    """Enable download links created by protected tools."""
    file_download_default_ttl_s: _PositiveInt = 3600
    """Default lifetime for file download links in seconds."""
    file_download_max_ttl_s: _PositiveInt = 604800
    """Maximum lifetime accepted for file download links in seconds."""
    file_download_default_max_downloads: _NonNegativeInt = 0
    """Default download-count limit for file links; 0 means unlimited until expiry."""
    file_download_max_file_bytes: _NonNegativeInt = 0
    """Maximum file size allowed for download links; 0 disables this size limit."""
    # Executor transport.
    executor_max_pending_commands: int = Field(default=64, ge=1)
    """Maximum queued or offered ordinary commands retained per executor."""
    executor_pairing_max_pending: int = Field(default=32, ge=1, le=1024)
    """Maximum live process-local executor pairing attempts admitted by control."""
    executor_pairing_ttl_s: int = Field(default=600, ge=60, le=24 * 3600)
    """Lifetime in seconds for one process-local executor pairing attempt."""

    # Agent capability bridge.
    agent_bridge_enabled: bool = True
    """Enable agent capability bridge tools."""
    agent_mcp_probe_timeout_s: _PositiveInt = 5
    """Agent MCP server probe timeout in seconds."""
    agent_mcp_call_timeout_s: _PositiveInt = 60
    """Agent MCP tool-call timeout in seconds."""
    agent_dynamic_mcp_tools: bool = True
    """Register dynamic MCP bridge tools."""

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
    def config_dir(self) -> Path:
        """Platform-native Workgate configuration namespace."""
        return app_paths().config_dir

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
        "default_workdir",
        "state_dir",
        "data_dir",
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

    @field_validator(
        "subprocess_env_blocklist",
        "subprocess_env_blocked_prefixes",
        mode="before",
    )
    @classmethod
    def normalize_subprocess_env_filters(cls, value: Any) -> list[str]:
        """Normalize comma-separated or list environment filter entries."""
        if value is None or value == "":
            return []
        entries = value.split(",") if isinstance(value, str) else value
        if not isinstance(entries, list | tuple):
            raise ValueError(
                "subprocess environment filters must be a list or comma-separated string"
            )
        normalized: list[str] = []
        for entry in entries:
            name = str(entry).strip()
            if not name:
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(
                    "subprocess environment filter entries must be environment variable names or prefixes"
                )
            if name not in normalized:
                normalized.append(name)
        return normalized

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """Normalize supported log levels before Literal validation."""
        return str(value).strip().lower()

    @model_validator(mode="after")
    def validate_limit_relationships(self) -> Settings:
        """Keep related runtime limits internally consistent."""
        if self.run_shell_max_timeout_s < self.run_shell_default_timeout_s:
            raise ValueError(
                "run_shell_max_timeout_s must be greater than or equal to "
                "run_shell_default_timeout_s"
            )
        if self.file_download_max_ttl_s < self.file_download_default_ttl_s:
            raise ValueError(
                "file_download_max_ttl_s must be greater than or equal to "
                "file_download_default_ttl_s"
            )
        if self.audit_inline_value_bytes > self.max_audit_payload_bytes:
            raise ValueError(
                "audit_inline_value_bytes must not exceed max_audit_payload_bytes"
            )
        if self.max_audit_payload_bytes > self.max_audit_payload_store_bytes:
            raise ValueError(
                "max_audit_payload_bytes must not exceed max_audit_payload_store_bytes"
            )
        if (
            self.max_transfer_payload_bytes
            > self.max_transfer_payload_store_bytes
        ):
            raise ValueError(
                "max_transfer_payload_bytes must not exceed "
                "max_transfer_payload_store_bytes"
            )
        return self


def _selected_setting_names(
    setting_names: Collection[str] | None,
) -> frozenset[str]:
    """Return validated Settings names selected for one process role."""
    fields = frozenset(Settings.model_fields)
    if setting_names is None:
        return fields
    selected = frozenset(setting_names)
    unknown = selected - fields
    if unknown:
        raise ValueError(f"Unknown Settings names: {sorted(unknown)}")
    return selected


def _default_setting_values() -> dict[str, Any]:
    """Materialize every default so BaseSettings cannot import foreign env fields."""
    return {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }


def read_config_file(
    path: str | Path | None,
    *,
    setting_names: Collection[str] | None = None,
) -> dict[str, Any]:
    """Read optional YAML configuration values for one role-owned field set."""
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
    known = frozenset(Settings.model_fields)
    unknown = frozenset(loaded) - known
    if unknown:
        raise ValueError(
            f"Unknown config settings in {config_path}: {sorted(unknown)}"
        )
    selected = _selected_setting_names(setting_names)
    loaded = {name: value for name, value in loaded.items() if name in selected}
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


def env_overrides(
    setting_names: Collection[str] | None = None,
) -> dict[str, Any]:
    """Return explicitly present environment values for one role-owned field set."""
    selected = _selected_setting_names(setting_names)
    present = {
        name: field_name
        for field_name in selected
        if (name := f"{ENV_PREFIX}{field_name.upper()}") in os.environ
    }
    if not present:
        return {}
    values = _default_setting_values()
    for env_name, field_name in present.items():
        values[field_name] = os.environ[env_name]
    parsed = Settings(**values)
    return {
        field_name: getattr(parsed, field_name)
        for field_name in present.values()
    }


def initialize_runtime_directories(settings: Settings) -> None:
    """Create the filesystem roots required by a configured runtime."""
    ensure_private_directory(settings.state_dir)
    ensure_private_directory(settings.audit_log_path.parent)


def load_settings(
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    setting_names: Collection[str] | None = None,
    default_config_path: str | Path | None = None,
    default_overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Load settings without mutating the runtime filesystem.

    When ``setting_names`` is provided, YAML/environment values owned by another
    process role are not imported into this process. Unselected fields retain
    model defaults.
    """
    selected = _selected_setting_names(setting_names)
    selected_config: str | Path | None = config_path
    if selected_config is None:
        selected_config = os.getenv("WORKGATE_CONFIG")
    if selected_config is None:
        default_config = (
            Path(default_config_path).expanduser()
            if default_config_path is not None
            else app_paths().config_file
        )
        selected_config = default_config if default_config.is_file() else None

    values = _default_setting_values()
    if default_overrides:
        foreign_defaults = frozenset(default_overrides) - selected
        if foreign_defaults:
            raise ValueError(
                "Settings role defaults are not owned by this role: "
                f"{sorted(foreign_defaults)}"
            )
        values.update(default_overrides)
    values.update(read_config_file(selected_config, setting_names=selected))
    values.update(env_overrides(selected))
    if overrides:
        foreign = frozenset(overrides) - selected
        if foreign:
            raise ValueError(
                f"Settings overrides are not owned by this role: {sorted(foreign)}"
            )
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
