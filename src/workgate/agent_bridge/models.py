"""Agent bridge configuration and registry data models."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SENSITIVE_KEY_PATTERN = (
    r"(?:authorization|cookie|credentials?|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"token|secret|password|passwd)"
)
SENSITIVE_KEY_RE = re.compile(SENSITIVE_KEY_PATTERN, re.I)


class AgentSecretReference(BaseModel):
    """Reference to a value held in the private Agent Bridge credential store."""

    model_config = ConfigDict(extra="forbid")
    """Reject unknown secret-reference fields."""

    secret: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Private credential-store key resolved for this server.",
    )


class AgentMcpAuthConfig(BaseModel):
    """Authentication mode and OAuth scopes for one upstream MCP server."""

    model_config = ConfigDict(extra="forbid")
    """Reject unknown authentication-policy fields."""

    mode: Literal["none", "secret", "oauth"] = "none"
    """Authentication mode applied to this upstream MCP server."""
    scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes requested during interactive authorization.",
    )

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: list[str]) -> list[str]:
        """Trim, validate, and deduplicate OAuth scope tokens in order."""
        normalized = []
        for scope in value:
            scope = scope.strip()
            if not scope or any(character.isspace() for character in scope):
                raise ValueError(
                    "OAuth scopes must be non-empty tokens without whitespace"
                )
            if scope not in normalized:
                normalized.append(scope)
        return normalized


AgentConfigValue = str | AgentSecretReference


class AgentMcpServerConfig(BaseModel):
    """Configuration for one upstream MCP server exposed through the agent bridge."""

    model_config = ConfigDict(populate_by_name=True)

    integration_id: str | None = Field(
        default=None,
        alias="integrationId",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description=(
            "Stable credential/redaction identity. Required when the server carries "
            "managed auth or credential-like literal env/header values."
        ),
    )

    type: Literal["stdio", "http", "sse"]
    """Transport type used to connect to the upstream MCP server."""
    enabled: bool = True
    """Whether this upstream server should be probed and exposed."""
    command: str | None = None
    """Executable command for stdio MCP servers."""
    args: list[str] = Field(default_factory=list)
    """Command-line arguments passed to a stdio MCP server."""
    env: dict[str, AgentConfigValue] = Field(default_factory=dict)
    """Environment variables injected into a stdio MCP server process."""
    url: str | None = None
    """HTTP or SSE endpoint URL for network MCP servers."""
    headers: dict[str, AgentConfigValue] = Field(default_factory=dict)
    """HTTP headers sent when connecting to network MCP servers."""
    auth: AgentMcpAuthConfig = Field(default_factory=AgentMcpAuthConfig)
    """Authentication policy for this upstream server."""

    @field_validator("command")
    @classmethod
    def non_empty_command(cls, value: str | None) -> str | None:
        """Reject blank stdio command values while allowing omitted commands."""
        if value is not None and not value.strip():
            raise ValueError("command must not be empty")
        return value

    @field_validator("url")
    @classmethod
    def non_empty_url(cls, value: str | None) -> str | None:
        """Reject blank network endpoint URLs while allowing omitted URLs."""
        if value is not None and not value.strip():
            raise ValueError("url must not be empty")
        return value

    @model_validator(mode="after")
    def validate_auth_transport(self) -> AgentMcpServerConfig:
        """Enforce transport and secret-reference consistency for auth modes."""
        secret_references = any(
            isinstance(value, AgentSecretReference)
            for value in (*self.env.values(), *self.headers.values())
        )
        if self.auth.mode == "oauth" and self.type not in {"http", "sse"}:
            raise ValueError(
                "OAuth authentication is valid only for HTTP or SSE MCP servers"
            )
        if self.auth.mode == "secret" and not secret_references:
            raise ValueError(
                "secret authentication requires at least one env or header secret reference"
            )
        if self.auth.mode != "secret" and secret_references:
            raise ValueError(
                "env or header secret references require auth.mode='secret'"
            )
        return self

    def requires_stable_integration_id(self) -> bool:
        """Return whether this server owns durable credential/redaction state."""
        sensitive_literals = any(
            isinstance(value, str) and SENSITIVE_KEY_RE.search(str(key))
            for mapping in (self.env, self.headers)
            for key, value in mapping.items()
        )
        return self.auth.mode in {"secret", "oauth"} or sensitive_literals


class AgentSkillsConfig(BaseModel):
    """Configuration for loading Markdown-based agent skills."""

    enabled: bool = True
    """Whether Markdown skill discovery is enabled."""
    directory: str = "skills"
    """Directory, relative to the agent config directory, containing skills."""


class AgentDynamicToolsConfig(BaseModel):
    """Feature flags for exposing discovered capabilities as public tools."""

    mcp: bool = True
    """Whether discovered upstream MCP tools become public dynamic tools."""
    skills: bool = True
    """Whether discovered Markdown skills become public dynamic tools."""


class AgentBridgeManifest(BaseModel):
    """Validated bridge manifest."""

    model_config = ConfigDict(populate_by_name=True)
    """Pydantic model configuration for manifest aliases."""

    version: int = 1
    """Manifest schema version supported by this bridge loader."""
    mcp_servers: dict[str, AgentMcpServerConfig] = Field(
        default_factory=dict, alias="mcpServers"
    )
    """Named upstream MCP server configurations."""
    skills: AgentSkillsConfig = Field(default_factory=AgentSkillsConfig)
    """Markdown skill discovery configuration."""
    dynamic_tools: AgentDynamicToolsConfig = Field(
        default_factory=AgentDynamicToolsConfig, alias="dynamicTools"
    )
    """Dynamic tool exposure settings for discovered capabilities."""

    @field_validator("version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        """Accept only manifest schema versions supported by this loader."""
        if value != 1:
            raise ValueError("version must be 1")
        return value

    @model_validator(mode="after")
    def unique_integration_ids(self) -> AgentBridgeManifest:
        """Require stable identities for credential-bearing configured integrations."""
        owners: dict[str, str] = {}
        for name, server in self.mcp_servers.items():
            integration_id = server.integration_id
            if (
                server.requires_stable_integration_id()
                and integration_id is None
            ):
                raise ValueError(
                    f"credential-bearing MCP server {name!r} requires a stable integrationId"
                )
            if integration_id is None:
                continue
            previous = owners.setdefault(integration_id, name)
            if previous != name:
                raise ValueError(
                    f"integrationId {integration_id!r} is shared by servers "
                    f"{previous!r} and {name!r}"
                )
        return self


@dataclass(frozen=True)
class LoadedAgentManifest:
    """Manifest load result."""

    config_path: Path
    """Path to the bridge manifest file."""
    status: Literal["missing_config", "invalid_config", "loaded"]
    """Load status for missing, invalid, or successfully parsed config."""
    data: AgentBridgeManifest = field(default_factory=AgentBridgeManifest)
    """Parsed manifest data, or defaults when config is absent or invalid."""
    errors: list[str] = field(default_factory=list)
    """Validation or parse errors collected while loading the manifest."""


@dataclass(frozen=True)
class SkillRecord:
    """Resolved skill metadata."""

    name: str
    """Stable skill name derived from its directory."""
    source: str
    """Registry source that supplied this skill."""
    source_path: str
    """Absolute path to the selected source's Skill root."""
    entry_path: str
    """Path to the Markdown skill entry file relative to the source config root."""
    description: str
    """Human-readable skill summary."""
    related_files: list[str]
    """Additional files related to the skill entry."""


@dataclass(frozen=True)
class SkillScanResult:
    """Skill discovery result."""

    skills: dict[str, SkillRecord] = field(default_factory=dict)
    """Accepted skills keyed by skill name."""

    warnings: list[str] = field(default_factory=list)
    """Non-fatal skill discovery warnings for ignored or invalid entries."""
    scanned_entries: int = 0
    """Filesystem entries consumed from the bounded scan budget."""
    path_bytes: int = 0
    """UTF-8 related-file path bytes consumed from the bounded path budget."""


@dataclass(frozen=True)
class AgentMcpServerRecord:
    """Probe result for one configured upstream MCP server."""

    name: str
    """Manifest key for the upstream MCP server."""
    config: AgentMcpServerConfig
    """Validated connection configuration for the server."""
    available: bool
    """Whether the server was reachable during probing."""
    tools: list[Any] = field(default_factory=list)
    """Probe-time sanitized capability metadata safe for public projection."""
    raw_tool_names: tuple[str, ...] = field(default=(), repr=False)
    """Raw upstream tool identities retained only for dispatch."""
    probe_redaction_values: tuple[str, ...] = field(default=(), repr=False)
    """Private credential values needed while raw probe identities remain callable."""
    error: str | None = None
    """Redacted probe error when the server is unavailable."""


@dataclass(frozen=True)
class SkillSource:
    """One ordered Skill registry root."""

    name: str
    """Stable source identifier returned to clients."""
    config_dir: Path
    """Root directory against which the relative Skill directory is resolved."""
    directory: str
    """Portable relative Skill directory under ``config_dir``."""

    @property
    def path(self) -> Path:
        """Return the normalized absolute Skill root without requiring it to exist."""
        return (
            self.config_dir.expanduser().resolve() / self.directory
        ).resolve()

    def public_row(self) -> dict[str, str]:
        """Return bounded source metadata suitable for public tool responses."""
        return {"source": self.name, "path": str(self.path)}


@dataclass(frozen=True)
class DynamicSkillToolRecord:
    """Association between a generated tool name and the skill it activates."""

    dynamic_name: str
    """Public dynamic tool name exposed by workgate."""
    skill_name: str
    """Underlying skill name activated by the dynamic tool."""


@dataclass(frozen=True)
class DynamicMcpToolRecord:
    """Association between a generated public tool name and an upstream MCP server tool."""

    dynamic_name: str
    """Public dynamic tool name exposed by workgate."""
    server_name: str
    """Manifest key for the upstream MCP server."""
    tool_name: str
    """Original upstream MCP tool name."""


@dataclass(frozen=True)
class AgentCapabilityRegistry:
    """Snapshot of discovered agent bridge capabilities."""

    config_dir: Path
    """Directory containing the managed bridge manifest."""
    project_root: Path
    """Project or explicit session workdir used for project-local Skills."""
    skill_sources: tuple[SkillSource, ...]
    """Ordered project, managed, and global Skill sources."""
    config_path: Path
    """Path to the bridge manifest file."""
    manifest_status: str
    """Load status for the current manifest."""
    manifest_errors: list[str]
    """Validation or parse errors collected while loading."""
    skills: dict[str, SkillRecord]
    """Discovered skills keyed by skill name."""
    skill_warnings: list[str]
    """Non-fatal skill discovery warnings."""
    mcp_servers: dict[str, AgentMcpServerRecord]
    """Probed upstream MCP servers keyed by manifest name."""
    dynamic_mcp_tools: bool
    """Whether dynamic MCP tool exposure is enabled."""
    dynamic_skill_tools: bool
    """Whether dynamic skill tool exposure is enabled."""
    dynamic_skill_tool_map: dict[str, DynamicSkillToolRecord]
    """Public skill tool names mapped to skill records."""
    dynamic_mcp_tool_map: dict[str, DynamicMcpToolRecord]
    """Public MCP tool names mapped to upstream tools."""
    client_manager: Any
    """MCP client-session manager used to call upstream tools."""
    include_project_skills: bool = True
    """Whether this snapshot includes the project/session-local Skill source."""
    mcp_server_types: frozenset[str] | None = None
    """Optional transport allowlist used to keep integration execution in its owner plane."""
    scan_skills: bool = True
    """Whether this registry snapshot owns filesystem-backed Skill discovery."""
