"""Central registry for configuration surfaces.

Settings field docstrings are the source of truth for application setting help
text; this registry controls grouping, CLI flags, and generated example
configuration files.
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Annotated, Any, Literal, cast, get_args, get_origin

from .settings import ENV_PREFIX, Settings

type SectionName = Literal[
    "Server",
    "Human interface",
    "Paths and state",
    "Authentication and OAuth",
    "Safety and resource limits",
    "Remote workers",
    "Agent capability bridge",
    "Tool executables",
]

SECTION_ORDER: tuple[SectionName, ...] = get_args(SectionName.__value__)
CLI_UNSET = object()


@dataclass(frozen=True)
class SettingSpec:
    """Specification for one setting of the server."""

    name: str
    """Settings attribute name on the Settings model."""
    section: SectionName
    """CLI group and config-file section where this setting is in."""
    help_override: str | None = None
    """Optional explicit help text; Settings field descriptions are used by default."""
    metavar: str | None = None
    """Optional placeholder shown for CLI arguments."""
    example_default: Any | None = None
    """Optional value used when rendering example configuration files."""
    dynamic_default_label: str | None = None
    """Stable description for defaults that are resolved at invocation time."""

    def __post_init__(self) -> None:
        """Run validation checks on the setting specification."""
        if not self.name.isidentifier():
            raise ValueError(f"Invalid setting name: {self.name}")
        if self.name not in Settings.model_fields:
            raise ValueError(
                f"Setting name not found in Settings model: {self.name}"
            )

    @property
    def help(self) -> str:
        """Return human-readable help text used by CLI and generated examples."""
        if self.help_override is not None:
            return self.help_override
        description = Settings.model_fields[self.name].description
        if description:
            return description
        raise RuntimeError(f"Missing Settings field docstring for {self.name}")

    @property
    def env_var(self) -> str:
        """Return the WORKGATE_* environment variable name for this setting."""
        return f"{ENV_PREFIX}{self.name.upper()}"

    @property
    def cli_flag(self) -> str:
        """Return the canonical CLI flag for this setting. For example `--base-url`"""
        return f"--{self.name.replace('_', '-')}"

    @property
    def unset_cli_flag(self) -> str:
        """Return the CLI flag that explicitly unsets this setting. For example, `--unset-base-url`"""
        return f"--unset-{self.name.replace('_', '-')}"

    @property
    def default(self) -> Any:
        """Return the actual Settings default value."""
        field = Settings.model_fields[self.name]
        return field.get_default(call_default_factory=True)

    @property
    def example(self) -> Any:
        """Return a deterministic example value for generated config files."""
        if self.example_default is not None:
            return self.example_default
        return self.default

    @property
    def annotation(self) -> Any:
        """Return this setting's annotation after unwrapping Annotated."""
        annotation = Settings.model_fields[self.name].annotation
        if get_origin(annotation) is Annotated:
            return get_args(annotation)[0]
        return annotation

    @property
    def is_nullable(self) -> bool:
        """Return whether this setting accepts None as a concrete value."""
        return type(None) in get_args(self.annotation)

    @property
    def non_none_annotation(self) -> Any:
        """Return this setting's annotation with an optional None member removed."""
        if not self.is_nullable:
            return self.annotation
        args = get_args(self.annotation)
        non_none_args = tuple(arg for arg in args if arg is not type(None))
        if len(non_none_args) == 1:
            return non_none_args[0]
        return self.annotation

    @property
    def argparse_type(self) -> type[int] | type[float] | type[str]:
        """Return an argparse type callable for this setting."""
        annotation = self.non_none_annotation
        if annotation in (int, float):
            return annotation
        return str

    @property
    def choices(self) -> tuple[str, ...] | None:
        """Return accepted string choices for this setting, when finite."""
        annotation = self.non_none_annotation
        if get_origin(annotation) is Literal:
            return tuple(str(item) for item in get_args(annotation))
        if self.is_bool:
            return ("true", "false")
        return None

    @property
    def is_bool(self) -> bool:
        """Return whether this setting is boolean."""
        return self.non_none_annotation is bool


SETTING_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("mode", "Server"),
    SettingSpec("host", "Server", metavar="HOST"),
    SettingSpec("port", "Server", metavar="PORT"),
    SettingSpec("ui_enabled", "Human interface"),
    SettingSpec("ui_path", "Human interface", metavar="PATH"),
    SettingSpec("ui_tui_command", "Human interface", metavar="COMMAND"),
    SettingSpec(
        "ui_terminal_idle_timeout_s",
        "Human interface",
        metavar="SECONDS",
    ),
    SettingSpec(
        "ui_terminal_max_connections",
        "Human interface",
        metavar="COUNT",
    ),
    SettingSpec("ui_wallpaper", "Human interface", metavar="MODE"),
    SettingSpec(
        "workspace_root",
        "Paths and state",
        metavar="PATH",
        example_default="/absolute/path/to/workspace",
        dynamic_default_label="invocation CWD",
    ),
    SettingSpec(
        "state_dir",
        "Paths and state",
        metavar="PATH",
        example_default="/absolute/path/to/workgate-state",
        dynamic_default_label="platform user-state directory",
    ),
    SettingSpec("auth_mode", "Authentication and OAuth"),
    SettingSpec("auth_bypass_localhost", "Authentication and OAuth"),
    SettingSpec(
        "mcp_session_idle_timeout_s",
        "Authentication and OAuth",
        metavar="SECONDS",
    ),
    SettingSpec(
        "mcp_max_sessions",
        "Authentication and OAuth",
        metavar="COUNT",
    ),
    SettingSpec("base_url", "Authentication and OAuth", metavar="URL"),
    SettingSpec("oauth_issuer", "Authentication and OAuth", metavar="URL"),
    SettingSpec(
        "oauth_resource",
        "Authentication and OAuth",
        metavar="URL",
    ),
    SettingSpec("oauth_admin_pin", "Authentication and OAuth", metavar="PIN"),
    SettingSpec(
        "oauth_access_token_ttl_s",
        "Authentication and OAuth",
        metavar="SECONDS",
    ),
    SettingSpec(
        "oauth_code_ttl_s",
        "Authentication and OAuth",
        metavar="SECONDS",
    ),
    SettingSpec(
        "oauth_max_pending_codes",
        "Authentication and OAuth",
        metavar="COUNT",
    ),
    SettingSpec(
        "oauth_client_ttl_s",
        "Authentication and OAuth",
        metavar="SECONDS",
    ),
    SettingSpec(
        "oauth_max_dynamic_clients",
        "Authentication and OAuth",
        metavar="COUNT",
    ),
    SettingSpec(
        "oauth_registration_max_body_bytes",
        "Authentication and OAuth",
        metavar="BYTES",
    ),
    SettingSpec(
        "oauth_registration_max_redirect_uris",
        "Authentication and OAuth",
        metavar="COUNT",
    ),
    SettingSpec(
        "oauth_registration_max_redirect_uri_chars",
        "Authentication and OAuth",
        metavar="CHARS",
    ),
    SettingSpec(
        "oauth_registration_max_client_name_chars",
        "Authentication and OAuth",
        metavar="CHARS",
    ),
    SettingSpec("allow_full_control", "Safety and resource limits"),
    SettingSpec(
        "tool_timeout_s", "Safety and resource limits", metavar="SECONDS"
    ),
    SettingSpec(
        "run_shell_default_timeout_s",
        "Safety and resource limits",
        metavar="SECONDS",
    ),
    SettingSpec(
        "run_shell_max_timeout_s",
        "Safety and resource limits",
        metavar="SECONDS",
    ),
    SettingSpec(
        "max_output_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec(
        "max_job_log_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec("max_jobs", "Safety and resource limits", metavar="COUNT"),
    SettingSpec(
        "max_agent_sessions",
        "Safety and resource limits",
        metavar="COUNT",
    ),
    SettingSpec(
        "agent_session_retention_s",
        "Safety and resource limits",
        metavar="SECONDS",
    ),
    SettingSpec(
        "max_session_snapshots",
        "Safety and resource limits",
        metavar="COUNT",
    ),
    SettingSpec(
        "max_session_snapshot_bytes",
        "Safety and resource limits",
        metavar="BYTES",
    ),
    SettingSpec(
        "max_file_read_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec(
        "max_view_image_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec("max_skills", "Safety and resource limits", metavar="COUNT"),
    SettingSpec(
        "max_skill_related_files",
        "Safety and resource limits",
        metavar="COUNT",
    ),
    SettingSpec(
        "max_skill_scan_entries",
        "Safety and resource limits",
        metavar="COUNT",
    ),
    SettingSpec(
        "max_skill_path_bytes",
        "Safety and resource limits",
        metavar="BYTES",
    ),
    SettingSpec(
        "max_file_write_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec(
        "max_grep_results", "Safety and resource limits", metavar="COUNT"
    ),
    SettingSpec(
        "max_directory_entries", "Safety and resource limits", metavar="COUNT"
    ),
    SettingSpec(
        "max_glob_results", "Safety and resource limits", metavar="COUNT"
    ),
    SettingSpec(
        "max_tree_entries", "Safety and resource limits", metavar="COUNT"
    ),
    SettingSpec("max_todos", "Safety and resource limits", metavar="COUNT"),
    SettingSpec(
        "max_todo_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec(
        "max_http_request_bytes",
        "Safety and resource limits",
        metavar="BYTES",
    ),
    SettingSpec(
        "max_audit_log_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec(
        "max_audit_event_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec("audit_payloads_enabled", "Safety and resource limits"),
    SettingSpec(
        "audit_inline_value_bytes",
        "Safety and resource limits",
        metavar="BYTES",
    ),
    SettingSpec(
        "max_audit_payload_bytes", "Safety and resource limits", metavar="BYTES"
    ),
    SettingSpec(
        "max_audit_payload_store_bytes",
        "Safety and resource limits",
        metavar="BYTES",
    ),
    SettingSpec(
        "audit_payload_retention_s",
        "Safety and resource limits",
        metavar="SECONDS",
    ),
    SettingSpec("max_tmp_files", "Safety and resource limits", metavar="COUNT"),
    SettingSpec("max_tmp_bytes", "Safety and resource limits", metavar="BYTES"),
    SettingSpec(
        "max_transfer_archive_entries",
        "Safety and resource limits",
        metavar="COUNT",
    ),
    SettingSpec(
        "max_transfer_unpacked_bytes",
        "Safety and resource limits",
        metavar="BYTES",
    ),
    SettingSpec(
        "max_concurrent_commands", "Safety and resource limits", metavar="COUNT"
    ),
    SettingSpec(
        "max_tmux_sessions", "Safety and resource limits", metavar="COUNT"
    ),
    SettingSpec("file_download_enabled", "Safety and resource limits"),
    SettingSpec(
        "file_download_default_ttl_s",
        "Safety and resource limits",
        metavar="SECONDS",
    ),
    SettingSpec(
        "file_download_max_ttl_s",
        "Safety and resource limits",
        metavar="SECONDS",
    ),
    SettingSpec(
        "file_download_default_max_downloads",
        "Safety and resource limits",
        metavar="COUNT",
    ),
    SettingSpec(
        "file_download_max_file_bytes",
        "Safety and resource limits",
        metavar="BYTES",
    ),
    SettingSpec(
        "command_denylist", "Safety and resource limits", metavar="CSV"
    ),
    SettingSpec("path_denylist", "Safety and resource limits", metavar="CSV"),
    SettingSpec(
        "executor_max_pending_commands",
        "Safety and resource limits",
        metavar="COUNT",
    ),
    SettingSpec("remote_enabled", "Remote workers"),
    SettingSpec("remote_invite_ttl_s", "Remote workers", metavar="SECONDS"),
    SettingSpec("remote_poll_timeout_s", "Remote workers", metavar="SECONDS"),
    SettingSpec("remote_job_timeout_s", "Remote workers", metavar="SECONDS"),
    SettingSpec("remote_max_pending_jobs", "Remote workers", metavar="COUNT"),
    SettingSpec("remote_http_transfer_enabled", "Remote workers"),
    SettingSpec(
        "remote_http_transfer_threshold_bytes",
        "Remote workers",
        metavar="BYTES",
    ),
    SettingSpec(
        "remote_http_transfer_chunk_bytes", "Remote workers", metavar="BYTES"
    ),
    SettingSpec(
        "remote_http_transfer_ticket_ttl_s", "Remote workers", metavar="SECONDS"
    ),
    SettingSpec(
        "remote_http_transfer_max_active", "Remote workers", metavar="COUNT"
    ),
    SettingSpec(
        "remote_http_transfer_max_spool_bytes",
        "Remote workers",
        metavar="BYTES",
    ),
    SettingSpec("agent_bridge_enabled", "Agent capability bridge"),
    SettingSpec(
        "agent_mcp_probe_timeout_s",
        "Agent capability bridge",
        metavar="SECONDS",
    ),
    SettingSpec(
        "agent_mcp_call_timeout_s", "Agent capability bridge", metavar="SECONDS"
    ),
    SettingSpec("agent_dynamic_mcp_tools", "Agent capability bridge"),
    SettingSpec("agent_dynamic_skill_tools", "Agent capability bridge"),
    SettingSpec("shell_executable", "Tool executables", metavar="PATH"),
    SettingSpec("tmux_bin", "Tool executables", metavar="PATH"),
    SettingSpec("rg_bin", "Tool executables", metavar="PATH"),
    SettingSpec("git_bin", "Tool executables", metavar="PATH"),
    SettingSpec("python_bin", "Tool executables", metavar="PATH"),
)

type SpecBySection = list[tuple[SectionName, list[SettingSpec]]]


def _group_setting_specs_by_section() -> SpecBySection:
    """Group setting specs by section while preserving registry order within each section."""
    specs_by_section: dict[SectionName, list[SettingSpec]] = {
        section: [] for section in SECTION_ORDER
    }
    for spec in SETTING_SPECS:
        specs_by_section[spec.section].append(spec)
    return list(specs_by_section.items())


SETTING_SPECS_BY_SECTION: SpecBySection = _group_setting_specs_by_section()
SPECS_BY_NAME: dict[str, SettingSpec] = {
    spec.name: spec for spec in SETTING_SPECS
}


def validate_setting_specs() -> None:
    """Ensure every Settings field has exactly one registry entry, and vice versa."""
    fields = set(Settings.model_fields)
    specs = set(SPECS_BY_NAME)
    missing = sorted(fields - specs)
    extra = sorted(specs - fields)
    if missing or extra:
        raise RuntimeError(
            f"Setting spec mismatch: missing={missing}, extra={extra}"
        )


def default_to_string(value: Any) -> str:
    """Render a default value for documentation, .env files, and argparse help."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, list):
        items = cast(list[Any], value)
        return ",".join(str(item) for item in items)
    return str(value)


def yaml_default(value: Any) -> Any:
    """Render a default value suitable for YAML dumping."""
    if isinstance(value, PurePath):
        return value.as_posix()
    return value


class BoolChoiceAction(argparse.Action):
    """Action for parsing boolean choice arguments."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if values not in ("true", "false"):
            raise argparse.ArgumentTypeError(f"Invalid boolean value: {values}")
        setattr(namespace, self.dest, values == "true")


def register_setting_cli_args(parser: argparse.ArgumentParser) -> None:
    """Register one CLI option for every Settings field, grouped by section."""
    validate_setting_specs()
    for section, specs in SETTING_SPECS_BY_SECTION:
        group = parser.add_argument_group(section)
        for spec in specs:
            default_display = (
                spec.dynamic_default_label
                or default_to_string(spec.default)
                or "unset"
            )
            kwargs: dict[str, Any] = {
                "dest": spec.name,
                "default": CLI_UNSET,
                "help": f"{spec.help} Overrides {spec.env_var} and config files. Default: {default_display}.",
            }
            if spec.metavar:
                kwargs["metavar"] = spec.metavar
            choices = spec.choices
            if choices:
                kwargs["choices"] = choices

            if (argparse_type := spec.argparse_type) in (int, float):
                kwargs["type"] = argparse_type
            elif spec.is_bool:
                kwargs["action"] = BoolChoiceAction

            target = group
            if spec.is_nullable:
                target = group.add_mutually_exclusive_group()
            target.add_argument(spec.cli_flag, **kwargs)
            if spec.is_nullable:
                target.add_argument(
                    spec.unset_cli_flag,
                    action="store_const",
                    const=None,
                    default=CLI_UNSET,
                    dest=spec.name,
                    help=(
                        f"Clear {spec.name.replace('_', '-')} by setting it to "
                        f"null. Overrides {spec.env_var} and config files."
                    ),
                )


def cli_overrides_from_args(args: argparse.Namespace) -> dict[str, object]:
    """Let given explicit CLI server options override Settings field."""
    validate_setting_specs()
    return {
        spec.name: value
        for spec in SETTING_SPECS
        if (value := getattr(args, spec.name, CLI_UNSET)) is not CLI_UNSET
    }
