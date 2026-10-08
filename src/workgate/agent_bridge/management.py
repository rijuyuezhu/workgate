"""Atomic management of the existing Agent Bridge manifest, without a second registry."""

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app_paths import ensure_private_directory
from ..utils.private_files import atomic_write_private_text, private_file_lock
from .models import (
    AgentMcpAuthConfig,
    AgentMcpServerConfig,
    AgentSecretReference,
)
from .state import load_agent_manifest


class ManagedMcpConfig(BaseModel):
    """Public MCP connection settings: credentials only by private-store key."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    integration_id: str | None = Field(
        default=None,
        alias="integrationId",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    type: Literal["stdio", "http", "sse"]
    enabled: bool = True
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, AgentSecretReference] = Field(default_factory=dict)
    headers: dict[str, AgentSecretReference] = Field(default_factory=dict)
    auth: AgentMcpAuthConfig = Field(default_factory=AgentMcpAuthConfig)

    @model_validator(mode="after")
    def no_inline_auth(self) -> ManagedMcpConfig:
        if self.url is not None:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "MCP URL must be http(s) without embedded credentials, query, or fragment"
                )
        if self.type == "stdio" and not self.command:
            raise ValueError("stdio MCP requires a command")
        if self.type in {"http", "sse"} and not self.url:
            raise ValueError("network MCP requires a URL")
        if self.type == "stdio" and (self.url is not None or self.headers):
            raise ValueError("stdio MCP does not use URL or headers")
        if self.type in {"http", "sse"} and (
            self.command or self.args or self.env
        ):
            raise ValueError("network MCP does not use command, args, or env")
        AgentMcpServerConfig.model_validate(self.model_dump(by_alias=True))
        return self


def _safe_row(name: str, config: AgentMcpServerConfig) -> dict[str, Any]:
    """Never echo legacy literal credentials or command arguments into model logs."""
    return {
        "name": name,
        "type": config.type,
        "enabled": config.enabled,
        "integrationId": config.integration_id,
        "auth": config.auth.model_dump(),
        "env": {
            key: value.model_dump()
            if isinstance(value, AgentSecretReference)
            else "<redacted>"
            for key, value in config.env.items()
        },
        "headers": {
            key: value.model_dump()
            if isinstance(value, AgentSecretReference)
            else "<redacted>"
            for key, value in config.headers.items()
        },
        # A manually edited legacy command/URL may embed credentials; never echo it.
    }


def manage_mcp_manifest(
    config_dir: Path,
    action: str,
    *,
    name: str | None = None,
    config: ManagedMcpConfig | None = None,
    owner_type: Literal["network", "stdio"],
) -> dict[str, Any]:
    """Read or atomically mutate one owner's existing config.json."""
    if action not in {
        "list",
        "get",
        "register",
        "update",
        "enable",
        "disable",
        "remove",
    }:
        raise ValueError(f"unsupported MCP management action: {action}")
    if action == "list" and name is not None:
        raise ValueError("name is not valid for action=list")
    if action != "list" and not name:
        raise ValueError(f"name required for {action}")
    if action in {"register", "update"} and config is None:
        raise ValueError(f"config required for {action}")
    if action not in {"register", "update"} and config is not None:
        raise ValueError(f"config is not valid for {action}")
    if config is not None and (config.type == "stdio") != (
        owner_type == "stdio"
    ):
        raise ValueError(
            f"{config.type} MCP must be managed by its owning plane"
        )
    if name and (len(name) > 128 or not name.strip()):
        raise ValueError("invalid MCP server name")

    config_dir = Path(config_dir)
    reading = action in {"list", "get"}
    if not reading:
        config_dir.mkdir(parents=True, exist_ok=True)
        ensure_private_directory(config_dir)
    with (
        nullcontext()
        if reading
        else private_file_lock(config_dir / "config.lock")
    ):
        loaded = load_agent_manifest(config_dir)
        if loaded.status == "invalid_config":
            raise ValueError(
                "Agent Bridge manifest is invalid; repair it before runtime changes"
            )
        servers = loaded.data.mcp_servers

        def is_owned(server: AgentMcpServerConfig) -> bool:
            return (server.type == "stdio") == (owner_type == "stdio")

        if action == "list":
            return {
                "servers": [
                    _safe_row(key, server)
                    for key, server in servers.items()
                    if is_owned(server)
                ]
            }
        assert name is not None
        current = servers.get(name)
        if action == "register":
            if current is not None:
                raise ValueError(f"MCP server already exists: {name}")
        elif current is None or not is_owned(current):
            raise ValueError(f"Unknown {owner_type} MCP server: {name}")

        if action == "get":
            assert current is not None
            return {"server": _safe_row(name, current)}
        if action == "register" or action == "update":
            assert config is not None
            if action == "update":
                assert current is not None
                if current.type != config.type:
                    raise ValueError(
                        "MCP transport cannot change through update"
                    )
                if (
                    current.integration_id
                    and current.integration_id != config.integration_id
                ):
                    raise ValueError(
                        "integrationId must be preserved when updating"
                    )
            servers[name] = AgentMcpServerConfig.model_validate(
                config.model_dump(by_alias=True)
            )
        elif action == "remove":
            del servers[name]
        else:
            assert current is not None
            servers[name] = current.model_copy(
                update={"enabled": action == "enable"}
            )

        # Validate whole manifest (including stable credential identities), while
        # preserving unrecognized configuration entries owned by the original file.
        from .models import AgentBridgeManifest

        path = loaded.config_path
        raw: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"version": 1}
        )
        raw_servers: dict[str, Any] = raw.setdefault("mcpServers", {})
        if action == "remove":
            del raw_servers[name]
        elif action in {"register", "update"}:
            assert config is not None
            raw_servers[name] = config.model_dump(
                by_alias=True, exclude_none=True
            )
        else:
            raw_servers[name]["enabled"] = action == "enable"
        AgentBridgeManifest.model_validate(raw)
        atomic_write_private_text(
            path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
        )
        return {"action": action, "name": name, "updated": True}
