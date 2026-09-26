"""Executor-owned Human UI terminal and raw-PTY operations."""

from typing import Any

from .shell_service import ShellService


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
        raise NotImplementedError(
            f"unsupported executor UI terminal operation: {op}"
        )
