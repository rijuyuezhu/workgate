"""Build agent bridge capability registries from manifests, skills, and MCP probes."""

import asyncio
import queue
import threading
from pathlib import Path
from typing import Any, cast

from .auth import (
    manager_redaction_cursor,
    manager_redaction_maps,
    manager_redaction_maps_since,
)
from .mcp import AgentMcpTool, normalize_mcp_tool
from .models import (
    AgentCapabilityRegistry,
    AgentMcpServerRecord,
    SkillScanResult,
)
from .redaction import redact_configured_value_tree
from .skills import (
    DEFAULT_MAX_ENTRY_BYTES,
    DEFAULT_MAX_PATH_BYTES,
    DEFAULT_MAX_RELATED_FILES,
    DEFAULT_MAX_SCAN_ENTRIES,
    DEFAULT_MAX_SKILLS,
)
from .sources import scan_skill_sources, skill_sources
from .state import load_agent_manifest


def _run_async_blocking(coro: Any, timeout_s: float | None = None) -> Any:
    """Run an async probe from synchronous registry-building code with a bounded timeout."""
    if timeout_s is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result_queue.put((True, asyncio.run(coro)))
        except BaseException as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        if timeout_s is None:
            success, result = result_queue.get()
        else:
            success, result = result_queue.get(timeout=timeout_s)
    except queue.Empty:
        raise TimeoutError(
            f"operation timed out after {timeout_s:g}s"
        ) from None
    if success:
        return result
    raise result


def _probe_timeout_seconds(probe_timeout_s: float) -> float:
    """Clamp the MCP probe timeout to a positive value before probing upstream servers."""
    return max(0.001, probe_timeout_s)


def _redaction_values(*maps: dict[str, str]) -> tuple[str, ...]:
    """Freeze unique private credential values used during one MCP probe."""
    return tuple(
        dict.fromkeys(
            value for mapping in maps for value in mapping.values() if value
        )
    )


def _sanitize_probe_tool(
    tool: Any, *redaction_maps: dict[str, str]
) -> AgentMcpTool:
    """Create public capability metadata without retaining probe-time credentials."""
    normalized = normalize_mcp_tool(tool)
    return AgentMcpTool(
        name=str(
            redact_configured_value_tree(normalized.name, *redaction_maps)
        ),
        description=str(
            redact_configured_value_tree(
                normalized.description, *redaction_maps
            )
        ),
        input_schema=cast(
            dict[str, Any],
            redact_configured_value_tree(
                normalized.input_schema, *redaction_maps
            ),
        ),
    )


def build_agent_registry(
    config_dir: Path,
    client_manager: Any | None = None,
    probe_timeout_s: float = 5,
    *,
    project_root: Path | None = None,
    max_skills: int = DEFAULT_MAX_SKILLS,
    max_skill_related_files: int = DEFAULT_MAX_RELATED_FILES,
    max_skill_scan_entries: int = DEFAULT_MAX_SCAN_ENTRIES,
    max_skill_path_bytes: int = DEFAULT_MAX_PATH_BYTES,
    max_skill_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    include_project_skills: bool = True,
    mcp_server_types: frozenset[str] | None = None,
    scan_skills: bool = True,
    probe_mcp_tools: bool = True,
) -> AgentCapabilityRegistry:
    """Build one manifest-backed registry with ordered project, managed, and global Skills."""
    config_root = Path(config_dir).expanduser().resolve()
    active_project_root = (
        config_root
        if project_root is None
        else Path(project_root).expanduser().resolve()
    )
    manifest = load_agent_manifest(config_root)
    sources = (
        skill_sources(
            project_root=active_project_root,
            managed_config_dir=config_root,
            managed_directory=manifest.data.skills.directory,
            include_project=include_project_skills,
        )
        if scan_skills
        else ()
    )
    probe_timeout = _probe_timeout_seconds(probe_timeout_s)
    if client_manager is None:
        from .mcp import AgentMcpClientManager

        client_manager = AgentMcpClientManager(call_timeout_s=probe_timeout)

    skill_scan = SkillScanResult()
    if (
        scan_skills
        and manifest.status != "invalid_config"
        and manifest.data.skills.enabled
    ):
        skill_scan = scan_skill_sources(
            sources,
            max_skills=max_skills,
            max_related_files=max_skill_related_files,
            max_scan_entries=max_skill_scan_entries,
            max_path_bytes=max_skill_path_bytes,
            max_entry_bytes=max_skill_entry_bytes,
        )

    mcp_servers: dict[str, AgentMcpServerRecord] = {}
    if manifest.status == "loaded":
        for name, server in manifest.data.mcp_servers.items():
            if (
                mcp_server_types is not None
                and server.type not in mcp_server_types
            ):
                continue
            if not server.enabled:
                mcp_servers[name] = AgentMcpServerRecord(
                    name=name,
                    config=server,
                    available=False,
                    error="disabled",
                )
                continue

            try:
                redaction_cursor = manager_redaction_cursor(
                    client_manager, name, server
                )
            except Exception:
                mcp_servers[name] = AgentMcpServerRecord(
                    name=name,
                    config=server,
                    available=False,
                    error="credential redaction history unavailable",
                )
                continue
            if not probe_mcp_tools:
                mcp_servers[name] = AgentMcpServerRecord(
                    name=name, config=server, available=True
                )
                continue
            before_env, before_headers = manager_redaction_maps(
                client_manager, name, server
            )
            try:
                tools = _run_async_blocking(
                    asyncio.wait_for(
                        client_manager.list_tools(name, server),
                        timeout=probe_timeout,
                    ),
                    timeout_s=probe_timeout,
                )
            except Exception as exc:
                try:
                    operation_env, operation_headers = (
                        manager_redaction_maps_since(
                            client_manager, name, server, redaction_cursor
                        )
                    )
                except Exception:
                    mcp_servers[name] = AgentMcpServerRecord(
                        name=name,
                        config=server,
                        available=False,
                        error="credential redaction history unavailable",
                    )
                    continue
                error = redact_configured_value_tree(
                    f"{type(exc).__name__}: {exc}",
                    before_env,
                    before_headers,
                    operation_env,
                    operation_headers,
                )
                mcp_servers[name] = AgentMcpServerRecord(
                    name=name,
                    config=server,
                    available=False,
                    error=str(error),
                )
                continue

            try:
                operation_env, operation_headers = manager_redaction_maps_since(
                    client_manager, name, server, redaction_cursor
                )
            except Exception:
                mcp_servers[name] = AgentMcpServerRecord(
                    name=name,
                    config=server,
                    available=False,
                    error="credential redaction history unavailable",
                )
                continue
            raw_tool_names = tuple(
                normalize_mcp_tool(tool).name for tool in tools
            )
            sanitized_tools = [
                _sanitize_probe_tool(
                    tool,
                    before_env,
                    before_headers,
                    operation_env,
                    operation_headers,
                )
                for tool in tools
            ]
            mcp_servers[name] = AgentMcpServerRecord(
                name=name,
                config=server,
                available=True,
                tools=sanitized_tools,
                raw_tool_names=raw_tool_names,
                probe_redaction_values=_redaction_values(
                    before_env,
                    before_headers,
                    operation_env,
                    operation_headers,
                ),
            )

    return AgentCapabilityRegistry(
        config_dir=config_root,
        project_root=active_project_root,
        skill_sources=sources,
        config_path=manifest.config_path,
        manifest_status=manifest.status,
        manifest_errors=manifest.errors,
        skills=skill_scan.skills,
        skill_warnings=skill_scan.warnings,
        mcp_servers=mcp_servers,
        client_manager=client_manager,
        include_project_skills=include_project_skills,
        mcp_server_types=mcp_server_types,
        scan_skills=scan_skills,
    )
