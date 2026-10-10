"""Executor-owned WebUI terminal and raw-PTY operations."""

from typing import Any

from .shell_service import ShellService


class UiTerminalsService:
    """Execute narrow WebUI terminal operations on this executor."""

    def __init__(self, shell: ShellService) -> None:
        self._shell = shell

    async def execute(self, op: str, args: dict[str, Any]) -> Any:
        session_scoped = bool(args.get("session_id"))
        if op == "ui.terminals.list":
            return (
                await self._shell.list(args)
                if session_scoped
                else await self._shell.list_all()
            )
        if op == "ui.terminals.start":
            return (
                await self._shell.start(args)
                if session_scoped
                else await self._shell.start_unowned(args)
            )
        if op == "ui.terminals.read":
            return (
                await self._shell.read(args)
                if session_scoped
                else await self._shell.read_unowned(args)
            )
        if op == "ui.terminals.kill":
            return (
                await self._shell.kill(args)
                if session_scoped
                else await self._shell.kill_unowned(args)
            )
        raise NotImplementedError(
            f"unsupported executor UI terminal operation: {op}"
        )
