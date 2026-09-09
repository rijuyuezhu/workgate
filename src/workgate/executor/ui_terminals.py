"""Executor-owned Human UI terminal and raw-PTY operations."""

from __future__ import annotations

from typing import Any

from .shell_service import ShellService
from .terminal.bridge import (
    close_terminal_bridge_execute,
    open_terminal_bridge_execute,
    read_terminal_bridge_execute,
    resize_terminal_bridge_execute,
    write_terminal_bridge_execute,
)


class UiTerminalsService:
    """Execute narrow Human UI terminal operations on this executor."""

    def __init__(self, shell: ShellService) -> None:
        self._shell = shell

    async def execute(self, op: str, args: dict[str, Any]) -> Any:
        if op == "ui.terminals.list":
            return await self._shell.list_all()
        if op == "ui.terminals.start":
            return await self._shell.start_unowned(args)
        if op == "ui.terminals.send":
            return await self._shell.send_unowned(args)
        if op == "ui.terminals.resize":
            return await self._shell.resize_unowned(args)
        if op == "ui.terminals.read":
            return await self._shell.read_unowned(args)
        if op == "ui.terminals.kill":
            return await self._shell.kill_unowned(args)
        if op == "ui.terminals.bridge.open":
            return await open_terminal_bridge_execute(
                str(args["shell_id"]), int(args["cols"]), int(args["rows"])
            )
        if op == "ui.terminals.bridge.read":
            return await read_terminal_bridge_execute(
                str(args["bridge_id"]),
                int(args.get("max_bytes") or 65_536),
                int(args.get("wait_ms") or 0),
            )
        if op == "ui.terminals.bridge.write":
            return await write_terminal_bridge_execute(
                str(args["bridge_id"]), str(args.get("data_b64") or "")
            )
        if op == "ui.terminals.bridge.resize":
            return await resize_terminal_bridge_execute(
                str(args["bridge_id"]), int(args["cols"]), int(args["rows"])
            )
        if op == "ui.terminals.bridge.close":
            return await close_terminal_bridge_execute(str(args["bridge_id"]))
        raise NotImplementedError(
            f"unsupported executor UI terminal operation: {op}"
        )
