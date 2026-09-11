"""Executor-local filesystem-backed Agent Skill operations."""

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..schemas.result_models.agent import (
    ActivateAgentSkillOutput,
    CallAgentMcpToolOutput,
    ListAgentMcpServersOutput,
    ListAgentMcpToolsOutput,
    ListAgentSkillsOutput,
    ReadAgentSkillFileOutput,
)
from .config import ExecutorConfig
from .tool_session.store import ToolSessionStore

if TYPE_CHECKING:
    from ..agent_bridge.models import SkillRecord
    from ..agent_bridge.sources import SkillSource


def _local_skill_registry(
    config: ExecutorConfig,
    store: ToolSessionStore,
    session_id: str,
) -> tuple[tuple[SkillSource, ...], dict[str, SkillRecord], list[str]]:
    """Scan bounded Skill sources under explicit executor/session authority."""
    from ..agent_bridge.models import SkillScanResult
    from ..agent_bridge.sources import scan_skill_sources, skill_sources
    from ..agent_bridge.state import load_agent_manifest

    session = store.touch_session(session_id)
    project_root = Path(session.workdir)
    manifest = load_agent_manifest(config.agent_config_dir)
    sources = skill_sources(
        project_root=project_root,
        managed_config_dir=config.agent_config_dir,
        managed_directory=manifest.data.skills.directory,
    )
    scan = SkillScanResult()
    if manifest.status != "invalid_config" and manifest.data.skills.enabled:
        scan = scan_skill_sources(
            sources,
            max_skills=config.max_skills,
            max_related_files=config.max_skill_related_files,
            max_scan_entries=config.max_skill_scan_entries,
            max_path_bytes=config.max_skill_path_bytes,
            max_entry_bytes=config.max_file_read_bytes,
        )
    return sources, scan.skills, scan.warnings


def _selected_skill_source(
    sources: tuple[SkillSource, ...], skill: SkillRecord
) -> SkillSource:
    """Return the exact source selected during the current bounded scan."""
    for source in sources:
        if (
            source.name == skill.source
            and str(source.path) == skill.source_path
        ):
            return source
    raise RuntimeError(f"Skill source is no longer available: {skill.source}")


def list_agent_skills_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    session_id: str,
) -> ListAgentSkillsOutput:
    """List Skills from the executor-owned session and local config sources."""
    sources, skills, warnings = _local_skill_registry(config, store, session_id)
    return ListAgentSkillsOutput(
        sources=[source.public_row() for source in sources],
        skills=[asdict(skill) for skill in skills.values()],
        warnings=warnings,
    )


def activate_agent_skill_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    name: str,
    session_id: str,
) -> ActivateAgentSkillOutput:
    """Load one exact Skill from the executor-owned session registry."""
    from ..agent_bridge.skills import activate_skill

    sources, skills, _warnings = _local_skill_registry(
        config, store, session_id
    )
    skill = skills.get(name)
    if skill is None:
        raise ValueError(f"Unknown agent skill: {name}")
    source = _selected_skill_source(sources, skill)
    return ActivateAgentSkillOutput(
        **activate_skill(
            source.config_dir,
            skill,
            max_entry_bytes=config.max_file_read_bytes,
        )
    )


def read_agent_skill_file_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    name: str,
    path: str,
    session_id: str,
) -> ReadAgentSkillFileOutput:
    """Read one bounded related file from the selected executor Skill source."""
    from ..agent_bridge.skills import read_agent_skill_file

    sources, skills, _warnings = _local_skill_registry(
        config, store, session_id
    )
    skill = skills.get(name)
    if skill is None:
        raise ValueError(f"Unknown agent skill: {name}")
    source = _selected_skill_source(sources, skill)
    return ReadAgentSkillFileOutput(
        **read_agent_skill_file(
            source.config_dir,
            name,
            path,
            source.directory,
            source_name=source.name,
            source_path=str(source.path),
            max_file_bytes=config.max_file_read_bytes,
        )
    )


class ExecutorAgentBridgeService:
    """Executor-owned stdio MCP integration runtime and private credential boundary."""

    def __init__(self, config: ExecutorConfig) -> None:
        from ..agent_bridge.auth_store import AgentAuthStore
        from ..agent_bridge.mcp import AgentMcpClientManager

        self._config = config
        self._manager = AgentMcpClientManager(
            config.agent_mcp_call_timeout_s,
            AgentAuthStore(config.agent_auth_dir),
            allow_stdio=True,
        )

    def _registry(self):
        """Build the current executor-local stdio-only capability snapshot."""
        from ..agent_bridge.registry import build_agent_registry

        return build_agent_registry(
            self._config.agent_config_dir,
            self._manager,
            self._config.agent_mcp_probe_timeout_s,
            False,
            False,
            project_root=self._config.agent_config_dir,
            max_skills=0,
            max_skill_related_files=self._config.max_skill_related_files,
            max_skill_scan_entries=self._config.max_skill_scan_entries,
            max_skill_path_bytes=self._config.max_skill_path_bytes,
            max_skill_entry_bytes=self._config.max_file_read_bytes,
            include_project_skills=False,
            mcp_server_types=frozenset({"stdio"}),
        )

    def list_servers(self) -> ListAgentMcpServersOutput:
        """List only stdio MCP servers configured on this executor."""
        from ..agent_bridge.service import list_agent_mcp_servers_payload

        return list_agent_mcp_servers_payload(self._registry())

    def list_tools(self, server: str | None = None) -> ListAgentMcpToolsOutput:
        """List tools from executor-local stdio MCP servers."""
        from ..agent_bridge.service import list_agent_mcp_tools_payload

        return list_agent_mcp_tools_payload(self._registry(), server)

    async def call_tool(
        self, server: str, tool: str, args: dict | None = None
    ) -> CallAgentMcpToolOutput:
        """Call one executor-local stdio MCP tool with executor-local secrets."""
        from ..agent_bridge.service import call_agent_mcp_tool_payload

        return await call_agent_mcp_tool_payload(
            self._registry(), server, tool, args or {}
        )

    def close(self) -> None:
        """Retire persistent stdio subprocesses owned by this executor."""
        self._manager.close()
