"""Executor-owned shell and persistent-terminal orchestration."""

from __future__ import annotations

from typing import Any

from ..schemas.result_models.shell import ListPersistentShellsOutput
from .bash import bash_execute, run_python_code_execute
from .config import ExecutorConfig
from .jobs import ExecutorJobService
from .shell import (
    authoritative_persistent_shell_ids_execute,
    kill_persistent_shell_execute,
    list_owned_persistent_shell_ids_execute,
    list_persistent_shells_execute,
    read_persistent_shell_output_execute,
    resize_persistent_shell_execute,
    send_persistent_shell_input_execute,
    start_persistent_shell_execute,
)
from .tool_session.store import ToolSessionStore


class ShellService:
    """Run shell operations from one resolved executor config and session store."""

    def __init__(self, config: ExecutorConfig, store: ToolSessionStore) -> None:
        self.config = config
        self.store = store
        self.jobs = ExecutorJobService(config, store)

    async def bash(self, args: dict[str, Any]) -> Any:
        return await bash_execute(
            self.config,
            self.store,
            str(args["session_id"]),
            str(args["command"]),
            str(args.get("cwd") or "."),
            args.get("timeout_s"),
            args.get("max_output_bytes"),
            args.get("env"),
            bool(args.get("async_", False)),
            bool(args.get("pty", False)),
            None if args.get("name") is None else str(args["name"]),
            job_start=self.jobs.start,
        )

    async def run_python_code(self, args: dict[str, Any]) -> Any:
        return await run_python_code_execute(
            self.config,
            self.store,
            str(args["session_id"]),
            str(args["code"]),
            str(args.get("cwd") or "."),
            args.get("timeout_s"),
            args.get("max_output_bytes"),
            args.get("env"),
            bool(args.get("async_", False)),
            bool(args.get("pty", False)),
            None if args.get("name") is None else str(args["name"]),
            job_start=self.jobs.start,
        )

    async def start(self, args: dict[str, Any]) -> Any:
        session_id = args.get("session_id")
        return await start_persistent_shell_execute(
            self.config,
            self.store,
            str(args.get("cwd") or "."),
            None if args.get("name") is None else str(args["name"]),
            None if args.get("command") is None else str(args["command"]),
            owner_session_id=None if session_id is None else str(session_id),
        )

    async def _require_owned(self, session_id: str, shell_id: str) -> set[str]:
        self.store.admit_active_session(session_id)
        owned = await list_owned_persistent_shell_ids_execute(
            self.config, self.store, session_id
        )
        if owned is None:
            raise RuntimeError(
                "persistent shell ownership is currently uncertain"
            )
        normalized = set(owned)
        if shell_id not in normalized:
            raise ValueError(
                f"shell_id {shell_id!r} is not owned by session {session_id!r}"
            )
        return normalized

    async def send(self, args: dict[str, Any]) -> Any:
        session_id = str(args["session_id"])
        shell_id = str(args["shell_id"])
        await self._require_owned(session_id, shell_id)
        return await send_persistent_shell_input_execute(
            self.config,
            shell_id,
            str(args.get("input_text") or ""),
            bool(args.get("enter", True)),
        )

    async def resize(self, args: dict[str, Any]) -> Any:
        session_id = str(args["session_id"])
        shell_id = str(args["shell_id"])
        await self._require_owned(session_id, shell_id)
        return await resize_persistent_shell_execute(
            self.config, shell_id, int(args["cols"]), int(args["rows"])
        )

    async def read(self, args: dict[str, Any]) -> Any:
        session_id = str(args["session_id"])
        shell_id = str(args["shell_id"])
        await self._require_owned(session_id, shell_id)
        preserve_ansi = args.get("preserve_ansi", False)
        if not isinstance(preserve_ansi, bool):
            raise ValueError("preserve_ansi must be a boolean")
        return await read_persistent_shell_output_execute(
            self.config,
            shell_id,
            int(args.get("lines") or 200),
            preserve_ansi=preserve_ansi,
        )

    async def kill(self, args: dict[str, Any]) -> Any:
        session_id = str(args["session_id"])
        shell_id = str(args["shell_id"])
        await self._require_owned(session_id, shell_id)
        return await kill_persistent_shell_execute(
            self.config, self.store, shell_id
        )

    async def list(self, args: dict[str, Any]) -> ListPersistentShellsOutput:
        session_id = str(args["session_id"])
        self.store.admit_active_session(session_id)
        owned = await list_owned_persistent_shell_ids_execute(
            self.config, self.store, session_id
        )
        if owned is None:
            raise RuntimeError(
                "persistent shell ownership is currently uncertain"
            )
        output = await list_persistent_shells_execute(self.config, self.store)
        owned_set = set(owned)
        return ListPersistentShellsOutput(
            shells=[
                shell for shell in output.shells if shell.shell_id in owned_set
            ]
        )

    async def list_all(self) -> ListPersistentShellsOutput:
        """List all executor-local shells for the internal Human UI surface."""
        return await list_persistent_shells_execute(self.config, self.store)

    async def stop_owned(self, session_id: str) -> list[str]:
        """Stop every persistent shell durably owned by one executor session."""
        discovered = await list_owned_persistent_shell_ids_execute(
            self.config, self.store, session_id
        )
        if discovered is None:
            raise RuntimeError(
                "persistent shell ownership could not be determined; refusing "
                "to remove the session"
            )
        self.store.reconcile_session_persistent_shells(
            session_id, set(discovered)
        )
        stopped: list[str] = []
        for shell_id in tuple(dict.fromkeys(discovered)):
            current = await list_owned_persistent_shell_ids_execute(
                self.config, self.store, session_id
            )
            if current is None:
                raise RuntimeError(
                    "persistent shell ownership could not be determined; "
                    "refusing to remove the session"
                )
            if shell_id not in current:
                self.store.reconcile_session_persistent_shells(
                    session_id, set(current)
                )
                continue
            result = await kill_persistent_shell_execute(
                self.config, self.store, shell_id
            )
            if not result.killed:
                active = await authoritative_persistent_shell_ids_execute(
                    self.config, self.store
                )
                if active is None or shell_id in active:
                    raise RuntimeError(
                        f"persistent shell could not be stopped: {shell_id}: "
                        f"{result.stderr or 'unknown backend error'}"
                    )
            stopped.append(shell_id)
        return stopped

    async def start_unowned(self, args: dict[str, Any]) -> Any:
        """Start an executor-local Human UI shell without public session ownership."""
        return await start_persistent_shell_execute(
            self.config,
            self.store,
            str(args.get("cwd") or "."),
            None if args.get("name") is None else str(args["name"]),
            None if args.get("command") is None else str(args["command"]),
        )

    async def send_unowned(self, args: dict[str, Any]) -> Any:
        return await send_persistent_shell_input_execute(
            self.config,
            str(args["shell_id"]),
            str(args.get("input_text") or ""),
            bool(args.get("enter", True)),
        )

    async def resize_unowned(self, args: dict[str, Any]) -> Any:
        return await resize_persistent_shell_execute(
            self.config,
            str(args["shell_id"]),
            int(args["cols"]),
            int(args["rows"]),
        )

    async def read_unowned(self, args: dict[str, Any]) -> Any:
        return await read_persistent_shell_output_execute(
            self.config,
            str(args["shell_id"]),
            int(args.get("lines") or 200),
            preserve_ansi=True,
        )

    async def kill_unowned(self, args: dict[str, Any]) -> Any:
        return await kill_persistent_shell_execute(
            self.config, self.store, str(args["shell_id"])
        )
