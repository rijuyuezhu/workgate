"""Typed structured outputs for agent bridge tools."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel


class AgentConfigStatusOutput(BaseModel):
    """Agent bridge configuration, Skill and MCP server status."""

    model_config = ConfigDict(extra="allow")
    """Allow passthrough keys for dynamically shaped output payloads."""

    config_dir: str = Field(description="Agent bridge configuration directory.")
    config_path: str = Field(description="Agent bridge manifest path.")
    manifest_status: str = Field(description="Manifest load status.")
    manifest_errors: list[str] = Field(
        description="Manifest validation or load errors."
    )
    skills: dict[str, Any] = Field(
        description="Skill discovery status summary."
    )
    mcp_servers: dict[str, Any] = Field(
        description="Configured upstream MCP server status rows."
    )


class ListAgentSkillsOutput(BaseModel):
    """Discovered agent skills from ordered project, managed, and global roots."""

    sources: list[dict[str, str]] = Field(
        description="Ordered Skill registry source rows."
    )
    skills: list[dict[str, Any]] = Field(
        description="Discovered skill metadata rows."
    )
    warnings: list[str] = Field(
        description="Non-fatal skill discovery warnings."
    )


class ActivateAgentSkillOutput(BaseModel):
    """Loaded agent skill instructions."""

    name: str = Field(description="Activated skill name.")
    source: str = Field(description="Selected Skill registry source.")
    source_path: str = Field(description="Absolute selected Skill source root.")
    entry_path: str = Field(
        description="Skill entry file path relative to the selected source config root."
    )
    description: str = Field(description="Skill summary description.")
    content: str = Field(
        description="Markdown instruction content for the skill."
    )
    bytes: int = Field(description="Original SKILL.md byte count.")
    related_files: list[str] = Field(
        description="Paths relative to the skill directory, excluding SKILL.md."
    )


class ReadAgentSkillFileOutput(BaseModel):
    """One bounded related text file from an agent skill."""

    name: str = Field(description="Selected skill name.")
    source: str = Field(description="Selected Skill registry source.")
    source_path: str = Field(description="Absolute selected Skill source root.")
    path: str = Field(description="Path relative to the skill directory.")
    content: str = Field(description="Normalized UTF-8 text content.")
    bytes: int = Field(description="Original file byte count.")


class ListAgentMcpServersOutput(RootModel[dict[str, Any]]):
    """Configured agent MCP server status rows keyed by server name."""


class ListAgentMcpToolsOutput(BaseModel):
    """Tools exposed by configured agent MCP servers."""

    tools: list[dict[str, Any]] = Field(
        description="Redacted upstream MCP tool rows."
    )


class SearchAgentMcpToolsOutput(BaseModel):
    """Small external-MCP search results without tool input schemas."""

    tools: list[dict[str, str]]
    total_matches: int


class InspectAgentMcpToolOutput(BaseModel):
    """One selected tool descriptor containing its complete input schema."""

    server: str
    tool: str
    description: str
    input_schema: dict[str, Any]


class CallAgentMcpToolOutput(BaseModel):
    """Redacted result from an upstream agent MCP tool call."""

    model_config = ConfigDict(extra="allow")
    """Allow upstream tool result keys after redaction."""
