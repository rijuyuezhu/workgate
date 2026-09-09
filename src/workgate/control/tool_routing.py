"""Control catalog adapters that route machine-facing tools to bound executors."""

from __future__ import annotations

from dataclasses import replace
from functools import wraps
from typing import Any

from mcp.types import CallToolResult

from ..remote.tool_specs import REMOTE_WORKER_TOOL_NAMES
from ..tools.contracts import McpToolContext, ToolRegistry
from ..tools.declarative import DeclarativeToolRegistry, ToolDefinition
from .downloads import ControlDownloadService
from .jobs import ControlJobService
from .session_copy import ControlSessionCopyService
from .sessions import ControlSessionCoordinator

_MACHINE_TOOL_NAMES = frozenset(
    {
        *REMOTE_WORKER_TOOL_NAMES,
        "view_image",
        "workspace_search",
        "fetch",
    }
)
_SESSION_CONTROL_TOOLS = frozenset(
    {"session_start", "session_change_cwd", "session_end", "session_copy"}
)
_DOWNLOAD_CONTROL_TOOLS = frozenset(
    {"create_file_link", "list_file_links", "revoke_file_link"}
)


class ControlToolRouter:
    """Single control-side dispatch seam for session and machine tool calls."""

    def __init__(
        self,
        sessions: ControlSessionCoordinator,
        session_copy: ControlSessionCopyService,
        jobs: ControlJobService,
        downloads: ControlDownloadService,
    ) -> None:
        self._sessions = sessions
        self._session_copy = session_copy
        self._jobs = jobs
        self._downloads = downloads

    async def invoke(self, tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "session_start":
            return await self._sessions.start_session(
                workdir=str(args["workdir"]),
                label=args.get("label"),
                executor_id=args.get("executor_id"),
            )
        if tool_name == "session_change_cwd":
            return await self._sessions.change_cwd(
                str(args["session_id"]), str(args["workdir"])
            )
        if tool_name == "session_end":
            return await self._sessions.end_session(
                str(args["session_id"]), force=bool(args.get("force", False))
            )
        if tool_name == "session_copy":
            copy_args = {
                "src_session_id": str(args["src_session_id"]),
                "src_path": str(args["src_path"]),
                "dst_session_id": str(args["dst_session_id"]),
                "dst_path": str(args["dst_path"]),
                "kind": args.get("kind", "auto"),
                "overwrite": bool(args.get("overwrite", True)),
                "chunk_size": args.get("chunk_size"),
            }
            if bool(args.get("background", False)):
                return await self._session_copy.start_background(**copy_args)
            return await self._session_copy.copy(**copy_args)
        if tool_name == "job":
            return await self._jobs.execute(
                session_id=str(args["session_id"]),
                list_jobs=bool(args.get("list_jobs", False)),
                poll=args.get("poll"),
                cancel=args.get("cancel"),
                retry=args.get("retry"),
                include_finished=bool(args.get("include_finished", True)),
                lines=int(args.get("lines", 200)),
            )
        if tool_name == "create_file_link":
            return await self._downloads.create(
                session_id=str(args["session_id"]),
                path=str(args["path"]),
                ttl_s=args.get("ttl_s"),
                filename=args.get("filename"),
                max_downloads=args.get("max_downloads"),
                inline=bool(args.get("inline", False)),
            )
        if tool_name == "list_file_links":
            return await self._downloads.list(
                session_id=str(args["session_id"]),
                include_expired=bool(args.get("include_expired", False)),
            )
        if tool_name == "revoke_file_link":
            return await self._downloads.revoke(
                session_id=str(args["session_id"]), token=str(args["token"])
            )
        result = await self._sessions.call_session_tool(tool_name, args)
        if tool_name == "view_image":
            return CallToolResult.model_validate(result)
        return result


def route_control_registry(
    registry: ToolRegistry,
    router: ControlToolRouter,
) -> ToolRegistry:
    """Replace only machine/session handlers while preserving public metadata."""
    if not isinstance(registry, DeclarativeToolRegistry):
        return registry
    if registry.name == "transfer":
        # These are executor-internal RPC primitives used by session_copy.  The
        # source registry intentionally keeps them off public HTTP/MCP surfaces.
        return registry
    route_names = (
        _MACHINE_TOOL_NAMES | _SESSION_CONTROL_TOOLS | _DOWNLOAD_CONTROL_TOOLS
    )
    if not any(tool.name in route_names for tool in registry._enabled_tools()):
        return registry
    return _RoutedDeclarativeRegistry(registry, router)


class _RoutedDeclarativeRegistry(ToolRegistry):
    def __init__(
        self,
        registry: DeclarativeToolRegistry,
        router: ControlToolRouter,
    ) -> None:
        self._registry = registry
        self._router = router
        self.name = registry.name

    def _enabled_tools(self) -> tuple[ToolDefinition, ...]:
        return tuple(
            self._route(tool) for tool in self._registry._enabled_tools()
        )

    def _route(self, tool: ToolDefinition) -> ToolDefinition:
        if tool.name not in (
            _MACHINE_TOOL_NAMES
            | _SESSION_CONTROL_TOOLS
            | _DOWNLOAD_CONTROL_TOOLS
        ):
            return tool

        @wraps(tool.func)
        async def routed(*args: Any, **kwargs: Any) -> Any:
            bound = tool.signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return await self._router.invoke(tool.name, dict(bound.arguments))

        return replace(tool, func=routed, session_admission="handler")

    def http_routes(self):
        return tuple(
            route
            for tool in self._enabled_tools()
            if (route := tool.http_route()) is not None
        )

    def http_handlers(self):
        return {
            tool.name: tool.http_handler() for tool in self._enabled_tools()
        }

    def register_mcp(self, mcp, context: McpToolContext) -> None:
        for tool in self._enabled_tools():
            tool.register_mcp(mcp, context)
        if (
            self.name == "agent_bridge"
            and context.settings.agent_bridge_enabled
        ):
            # Preserve control-owned dynamic upstream-MCP integration tools while
            # the filesystem-backed static Skill tools above use executor routing.
            from ..tools.registry.agent import register_agent_bridge_dynamic_mcp

            register_agent_bridge_dynamic_mcp(mcp, context)


def control_machine_tool_names() -> frozenset[str]:
    """Expose the final machine-facing public classification for architecture tests."""
    return _MACHINE_TOOL_NAMES
