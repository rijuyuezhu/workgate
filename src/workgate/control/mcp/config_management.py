"""Model-facing configuration management shares WebUI's role-owned backend."""

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ...config.editing import ConfigEditError, save_config
from ...config.inspection import current_startup_config
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import (
    SCOPE_EXECUTOR_USE,
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from ...tools.metadata import mcp_security_meta, tool_safety_annotations
from ..runtime import ControlRuntime


def register_config_management(
    mcp: MCPServer, runtime: ControlRuntime | None
) -> None:
    """Expose revision-based config inspect/save without needing a local Executor."""
    if runtime is None:
        return

    def authorize(scopes: tuple[str, ...]) -> None:
        try:
            require_oauth_scopes(scopes)
        except MissingOAuthScopeError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(
        description="Read the running Control configuration or one online Executor's configuration and file revision.",
        annotations=tool_safety_annotations("config_inspect", read_only=True),
        meta=mcp_security_meta(
            (SCOPE_SHELL_READ, SCOPE_EXECUTOR_USE), settings=runtime.config
        ),
        structured_output=True,
    )
    async def config_inspect(executor_id: str | None = None) -> dict[str, Any]:
        authorize(
            (SCOPE_SHELL_READ, SCOPE_EXECUTOR_USE)
            if executor_id
            else (SCOPE_SHELL_READ,)
        )
        if not executor_id:
            snapshot = current_startup_config("control")
            if snapshot is None:
                raise ToolError("Control startup configuration unavailable")
            return await asyncio.to_thread(snapshot.describe)
        try:
            reply = await runtime.executor_transport.call(
                executor_id, "ui.config.inspect", {}, timeout_s=8
            )
            if not reply.ok or not isinstance(reply.result, dict):
                raise ToolError("Executor configuration unavailable")
            return reply.result
        except Exception:
            raise ToolError("Executor configuration unavailable") from None

    @mcp.tool(
        description="Atomically save role-owned configuration file changes using a revision from config_inspect. Saves file only; does NOT restart or apply.",
        annotations=tool_safety_annotations("config_save", read_only=False),
        meta=mcp_security_meta(
            (SCOPE_SHELL_WRITE, SCOPE_EXECUTOR_USE), settings=runtime.config
        ),
        structured_output=True,
    )
    async def config_save(
        revision: str, changes: dict[str, Any], executor_id: str | None = None
    ) -> dict[str, str]:
        authorize(
            (SCOPE_SHELL_WRITE, SCOPE_EXECUTOR_USE)
            if executor_id
            else (SCOPE_SHELL_WRITE,)
        )
        try:
            if not executor_id:
                snapshot = current_startup_config("control")
                if snapshot is None:
                    raise ToolError("Control startup configuration unavailable")
                updated = await asyncio.to_thread(
                    save_config, snapshot, revision=revision, changes=changes
                )
                return {"revision": updated}
            reply = await runtime.executor_transport.call(
                executor_id,
                "ui.config.save",
                {"revision": revision, "changes": changes},
                timeout_s=12,
            )
            if not reply.ok or not isinstance(reply.result, dict):
                raise ToolError("Executor config save failed")
            if "error" in reply.result:
                code = reply.result["error"]
                if code in {
                    "RevisionConflict",
                    "InvalidConfigValue",
                    "InvalidEdit",
                    "NoConfigFile",
                    "ConfigNotWritable",
                    "UnsafeConfigPath",
                    "InvalidConfigFile",
                }:
                    raise ToolError(code)
                raise ToolError("Executor config save failed")
            return {"revision": str(reply.result["revision"])}
        except ConfigEditError as exc:
            raise ToolError(exc.code) from None
        except ToolError:
            raise
        except Exception:
            raise ToolError("Configuration save unavailable") from None
