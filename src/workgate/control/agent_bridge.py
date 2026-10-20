"""Control-owned Agent Bridge routing across network and executor-local transports."""

import asyncio
from dataclasses import asdict
from typing import Any

from ..agent_bridge.discovery import McpDiscovery
from ..agent_bridge.management import ManagedMcpConfig, manage_mcp_manifest
from ..agent_bridge.mcp import AgentMcpClientManager
from ..agent_bridge.models import (
    AgentCapabilityRegistry,
    SkillScanResult,
    SkillSource,
)
from ..agent_bridge.service import (
    agent_config_status_payload,
    build_network_agent_registry_from_settings,
    call_agent_mcp_tool_payload,
    list_agent_mcp_servers_payload,
    list_agent_mcp_tools_payload,
)
from ..agent_bridge.skills import (
    DEFAULT_MAX_SKILLS,
    activate_skill,
    read_agent_skill_file,
    scan_agent_skills,
)
from ..agent_bridge.state import load_agent_manifest
from ..config.control import ControlConfig
from ..errors import PublicToolValueError
from ..schemas.result_models.agent import (
    ActivateAgentSkillOutput,
    AgentConfigStatusOutput,
    CallAgentMcpToolOutput,
    ListAgentMcpServersOutput,
    ListAgentMcpToolsOutput,
    ListAgentSkillsOutput,
    ReadAgentSkillFileOutput,
)
from .sessions import ControlSessionCoordinator


class ControlAgentBridgeService:
    """Keep network MCP on control and route session-bound stdio MCP to executors."""

    def __init__(
        self, settings: ControlConfig, sessions: ControlSessionCoordinator
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._discovery = McpDiscovery(self._discovery_rows)

    def _control_skills(self) -> tuple[SkillSource, SkillScanResult]:
        """Read only the Control-owned root, never an Executor's filesystem."""
        manifest = load_agent_manifest(
            self._settings.agent_config_dir, read_only=True
        )
        source = SkillSource(
            "control",
            self._settings.agent_config_dir,
            manifest.data.skills.directory,
        )
        if manifest.status == "invalid_config":
            return source, SkillScanResult(
                warnings=["Control Skill manifest invalid"]
            )
        if not manifest.data.skills.enabled:
            return source, SkillScanResult()
        return source, scan_agent_skills(
            source.config_dir,
            source.directory,
            source_name="control",
            source_path=str(source.path),
            excluded_names=frozenset(manifest.data.skills.disabled),
        )

    async def config_status(self) -> AgentConfigStatusOutput:
        """Keep public Control Skill counts consistent with Control Skill listing."""
        registry = await asyncio.to_thread(self._network_registry)
        status = agent_config_status_payload(registry)
        skills = await self.list_skills(None)
        status.skills = {
            "count": len(skills.skills),
            "warnings": skills.warnings,
            "sources": skills.sources,
        }
        return status

    async def list_skills(
        self, session_id: str | None
    ) -> ListAgentSkillsOutput:
        """Merge Session sources; support Control-only lookup without an Executor."""
        executor = (
            ListAgentSkillsOutput.model_validate(
                await self._sessions.call_session_tool(
                    "list_agent_skills", {"session_id": session_id}
                )
            )
            if session_id is not None
            else ListAgentSkillsOutput(sources=[], skills=[], warnings=[])
        )
        source, scan = await asyncio.to_thread(self._control_skills)
        skills = list(executor.skills)
        names = {row["name"] for row in skills}
        warnings = list(executor.warnings)
        for name, record in scan.skills.items():
            if name in names:
                if len(warnings) < 100:
                    warnings.append(
                        f"control: skipped duplicate Skill {name!r}; "
                        "a higher-priority source already provides it"
                    )
            elif len(skills) < DEFAULT_MAX_SKILLS:
                skills.append(asdict(record))
            elif len(warnings) < 100:
                warnings.append("Combined Skill list truncated")
                break
        warnings.extend(scan.warnings[: max(0, 100 - len(warnings))])
        return ListAgentSkillsOutput(
            sources=[*executor.sources, source.public_row()],
            skills=skills,
            warnings=warnings[:100],
        )

    async def _skill_owner(self, session_id: str | None, name: str):
        """Resolve afresh under Session authority or Control-only scope."""
        if session_id is not None:
            executor = ListAgentSkillsOutput.model_validate(
                await self._sessions.call_session_tool(
                    "list_agent_skills", {"session_id": session_id}
                )
            )
            if any(row["name"] == name for row in executor.skills):
                return None
        source, scan = await asyncio.to_thread(self._control_skills)
        record = scan.skills.get(name)
        if record is None:
            raise ValueError(f"Unknown agent skill: {name}")
        return source, record

    async def activate_skill(
        self, session_id: str | None, name: str
    ) -> ActivateAgentSkillOutput:
        selected = await self._skill_owner(session_id, name)
        if selected is None:
            payload = await self._sessions.call_session_tool(
                "activate_agent_skill", {"session_id": session_id, "name": name}
            )
            return ActivateAgentSkillOutput.model_validate(payload)
        source, record = selected
        return ActivateAgentSkillOutput.model_validate(
            await asyncio.to_thread(activate_skill, source.config_dir, record)
        )

    async def read_skill_file(
        self, session_id: str | None, name: str, path: str
    ) -> ReadAgentSkillFileOutput:
        selected = await self._skill_owner(session_id, name)
        if selected is None:
            payload = await self._sessions.call_session_tool(
                "read_agent_skill_file",
                {"session_id": session_id, "name": name, "path": path},
            )
            return ReadAgentSkillFileOutput.model_validate(payload)
        source, record = selected
        return ReadAgentSkillFileOutput.model_validate(
            await asyncio.to_thread(
                read_agent_skill_file,
                source.config_dir,
                name,
                path,
                source.directory,
                source_name="control",
                source_path=str(source.path),
            )
        )

    def _network_registry(
        self,
        *,
        probe_mcp_tools: bool = True,
        mcp_server_name: str | None = None,
        excluded_mcp_server_names: frozenset[str] = frozenset(),
    ) -> AgentCapabilityRegistry:
        return build_network_agent_registry_from_settings(
            self._settings,
            AgentMcpClientManager,
            probe_mcp_tools=probe_mcp_tools,
            mcp_server_name=mcp_server_name,
            excluded_mcp_server_names=excluded_mcp_server_names,
        )

    @staticmethod
    def _require_session_id(session_id: str | None, server: str) -> str:
        if session_id is None:
            raise PublicToolValueError(
                f"Unknown agent MCP server: {server}; "
                "pass session_id for executor-local stdio MCP servers"
            )
        return session_id

    async def _resolve_server_owner(
        self,
        server: str,
        session_id: str | None,
        *,
        include_disabled: bool = False,
    ) -> tuple[AgentCapabilityRegistry, str]:
        registry = await asyncio.to_thread(
            self._network_registry, probe_mcp_tools=False
        )
        control_has = server in registry.mcp_servers
        if session_id is None:
            return registry, "control" if control_has else "unknown"
        payload = await self._sessions.call_session_tool(
            "agent_mcp.list_servers",
            {"session_id": session_id, "probe_mcp_tools": False},
        )
        executor_rows = ListAgentMcpServersOutput.model_validate(payload).root
        executor_row = executor_rows.get(server)
        # Disabled entries do not mask lower-priority sources, as with Skills.
        # Management still needs to address a disabled Executor entry.
        if executor_row is not None and (
            include_disabled
            or not isinstance(executor_row, dict)
            or executor_row.get("enabled") is not False
        ):
            return registry, "executor"
        if control_has:
            return registry, "control"
        return registry, "unknown"

    @staticmethod
    def _merge_server_rows(
        control_rows: dict[str, Any], executor_rows: dict[str, Any]
    ) -> dict[str, Any]:
        merged = dict(control_rows)
        for name, row in executor_rows.items():
            if (
                name not in merged
                or not isinstance(row, dict)
                or row.get("enabled") is not False
            ):
                merged[name] = row
        return merged

    async def list_servers(
        self, session_id: str | None = None
    ) -> ListAgentMcpServersOutput:
        if session_id is None:
            registry = await asyncio.to_thread(self._network_registry)
            return list_agent_mcp_servers_payload(registry)
        payload = await self._sessions.call_session_tool(
            "agent_mcp.list_servers", {"session_id": session_id}
        )
        executor = ListAgentMcpServersOutput.model_validate(payload).root
        excluded = frozenset(
            name
            for name, row in executor.items()
            if not isinstance(row, dict) or row.get("enabled") is not False
        )
        control = list_agent_mcp_servers_payload(
            await asyncio.to_thread(
                self._network_registry,
                excluded_mcp_server_names=excluded,
            )
        ).root
        return ListAgentMcpServersOutput(
            root=self._merge_server_rows(control, executor)
        )

    async def list_tools(
        self, server: str | None = None, session_id: str | None = None
    ) -> ListAgentMcpToolsOutput:
        if server is not None:
            _, owner = await self._resolve_server_owner(server, session_id)
            if owner == "control":
                registry = await asyncio.to_thread(
                    self._network_registry, mcp_server_name=server
                )
                return list_agent_mcp_tools_payload(registry, server)
            selected_session = self._require_session_id(session_id, server)
            if owner == "unknown":
                raise ValueError(f"Unknown agent MCP server: {server}")
            payload = await self._sessions.call_session_tool(
                "agent_mcp.list_tools",
                {"session_id": selected_session, "server": server},
            )
            return ListAgentMcpToolsOutput.model_validate(payload)

        if session_id is None:
            registry = await asyncio.to_thread(self._network_registry)
            return list_agent_mcp_tools_payload(registry)

        # Choose the effective owner before any Control network probe.
        # Disabled Executor entries do not shadow the Control fallback.
        inventory = await self._sessions.call_session_tool(
            "agent_mcp.list_servers",
            {"session_id": session_id, "probe_mcp_tools": False},
        )
        executor_names = frozenset(
            name
            for name, row in ListAgentMcpServersOutput.model_validate(
                inventory
            ).root.items()
            if not isinstance(row, dict) or row.get("enabled") is not False
        )
        registry = await asyncio.to_thread(
            self._network_registry,
            excluded_mcp_server_names=executor_names,
        )
        control = list_agent_mcp_tools_payload(registry).tools
        payload = await self._sessions.call_session_tool(
            "agent_mcp.list_tools", {"session_id": session_id, "server": None}
        )
        executor = ListAgentMcpToolsOutput.model_validate(payload).tools
        return ListAgentMcpToolsOutput(tools=[*control, *executor])

    async def _discovery_rows(
        self, session_id: str | None, server: str | None
    ) -> list[dict[str, Any]]:
        return (
            await self.list_tools(server=server, session_id=session_id)
        ).tools

    def invalidate_discovery(self, session_id: str) -> None:
        self._discovery.invalidate(session_id)

    def invalidate_all_discovery(self) -> None:
        self._discovery.invalidate_all()

    async def manage(
        self,
        action: str,
        *,
        name: str | None = None,
        config: ManagedMcpConfig | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Manage one owner's existing manifest; route stdio through its session."""
        if action not in {"register", "update"} and config is not None:
            raise ValueError(f"config is not valid for {action}")
        if action == "list" and name is not None:
            raise ValueError("name is not valid for action=list")
        if action in {"register", "update"} and config is None:
            raise ValueError(f"config is required for {action}")
        if action == "list":
            control = await asyncio.to_thread(
                manage_mcp_manifest,
                self._settings.agent_config_dir,
                "list",
                owner_type="network",
            )
            if session_id is None:
                return {
                    "servers": [
                        {**row, "owner": "control"}
                        for row in control["servers"]
                    ]
                }
            executor = await self._sessions.call_session_tool(
                "agent_mcp.manage", {"session_id": session_id, "action": "list"}
            )
            if not isinstance(executor, dict):
                raise ValueError(
                    "executor returned invalid MCP management payload"
                )
            executor_servers = executor.get("servers")
            if not isinstance(executor_servers, list):
                raise ValueError(
                    "executor returned invalid MCP management catalog"
                )
            # Management lists every configured owner, unlike effective
            # discovery. Explicit owner labels disambiguate colliding names.
            return {
                "servers": [
                    *(
                        {**row, "owner": "control"}
                        for row in control["servers"]
                    ),
                    *(
                        {**row, "owner": "executor"}
                        for row in executor_servers
                        if isinstance(row, dict)
                        and isinstance(row.get("name"), str)
                    ),
                ]
            }

        if not name:
            raise ValueError(f"name is required for {action}")
        if action == "register":
            if config is None:
                raise ValueError("config is required for register")
            owner = "executor" if config.type == "stdio" else "control"
            _, existing = await self._resolve_server_owner(
                name,
                session_id if owner == "executor" else None,
                include_disabled=True,
            )
            if existing == owner:
                raise ValueError(f"MCP server already exists: {name}")
        else:
            _, owner = await self._resolve_server_owner(
                name, session_id, include_disabled=True
            )
            if owner == "unknown":
                raise ValueError(f"Unknown agent MCP server: {name}")
        if action == "refresh":
            self._discovery.invalidate_all()
            return await self.search_tools(
                "", server=name, session_id=session_id, refresh=True
            )

        if owner == "control":
            result = await asyncio.to_thread(
                manage_mcp_manifest,
                self._settings.agent_config_dir,
                action,
                name=name,
                config=config,
                owner_type="network",
                auth_dir=self._settings.agent_auth_dir,
            )
        else:
            if session_id is None:
                raise ValueError(
                    "session_id is required for executor-owned stdio MCP"
                )
            result = await self._sessions.call_session_tool(
                "agent_mcp.manage",
                {
                    "session_id": session_id,
                    "action": action,
                    "name": name,
                    "config": config.model_dump(by_alias=True)
                    if config
                    else None,
                },
            )
        if action not in {"get"}:
            self._discovery.invalidate_all()
        if not isinstance(result, dict):
            raise ValueError("MCP management returned invalid payload")
        return result

    async def search_tools(
        self,
        query: str = "",
        *,
        session_id: str | None = None,
        server: str | None = None,
        limit: int = 20,
        refresh: bool = False,
    ) -> dict[str, Any]:
        return await self._discovery.search(
            query,
            session_id=session_id,
            server=server,
            limit=limit,
            refresh=refresh,
        )

    async def inspect_tool(
        self,
        server: str,
        tool: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._discovery.inspect(
            server, tool, session_id=session_id
        )

    async def call_tool(
        self,
        server: str,
        tool: str,
        args: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> CallAgentMcpToolOutput:
        registry, owner = await self._resolve_server_owner(server, session_id)
        if owner == "control":
            return await call_agent_mcp_tool_payload(
                registry, server, tool, args or {}
            )
        selected_session = self._require_session_id(session_id, server)
        if owner == "unknown":
            raise PublicToolValueError(f"Unknown agent MCP server: {server}")
        payload = await self._sessions.call_session_tool(
            "agent_mcp.call_tool",
            {
                "session_id": selected_session,
                "server": server,
                "tool": tool,
                "args": args or {},
            },
        )
        return CallAgentMcpToolOutput.model_validate(payload)
