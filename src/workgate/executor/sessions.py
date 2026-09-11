"""Executor-authoritative shared session resources for protocol v1."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..jobs.state import CONFIRMED_TERMINAL_STATUSES
from ..protocol.executor import SessionInventorySummary
from .config import ExecutorConfig
from .errors import ExecutorOperationFailure
from .path import resolve_path_with_policy
from .session_orientation import change_session_cwd, session_output
from .shell_service import ShellService
from .tool_session.lifecycle import session_lifecycle_lock
from .tool_session.store import ToolSessionStore, UnknownAgentSessionError


class ExecutorSessionService:
    """Own final shared session IDs and their executor-side durable state."""

    def __init__(
        self,
        config: ExecutorConfig,
        store: ToolSessionStore,
        shell: ShellService,
    ) -> None:
        self._config = config
        self._store = store
        self._shell = shell

    def inventory(self) -> tuple[SessionInventorySummary, ...]:
        """Return the complete final-session inventory for hello reconciliation."""
        return tuple(
            self._summary(session.session_id, session.workdir)
            for session in self._store.list_sessions()
            if session.session_id.startswith("sess_")
        )

    def lookup(self, session_id: str) -> SessionInventorySummary | None:
        """Return one positive read-only shared-session observation, if present."""
        try:
            session = self._store.require_session(session_id)
        except UnknownAgentSessionError:
            return None
        return self._summary(session.session_id, session.workdir)

    async def create(
        self,
        session_id: str,
        *,
        workdir: str,
        label: str | None,
    ) -> Any:
        """Create one executor session under the control-allocated shared ID."""
        resolved = self._resolve_workdir(workdir)
        try:
            session = self._store.create_session(
                session_id=session_id,
                workdir=resolved,
                label=label,
            )
            return await asyncio.to_thread(
                session_output, self._config, session
            )
        except BaseException as exc:
            if self._confirm_absent_after_failed_create(session_id):
                raise ExecutorOperationFailure(
                    "session_create_absent", str(exc) or type(exc).__name__
                ) from exc
            raise ExecutorOperationFailure(
                "session_create_unconfirmed",
                "session creation failed and executor could not confirm absence",
            ) from exc

    async def terminate(self, session_id: str) -> dict[str, Any]:
        """Converge one shared session to desired absence idempotently."""
        try:
            async with session_lifecycle_lock(session_id):
                self._store.prepare_session_termination(session_id)
                stopped_jobs = await self._stop_owned_jobs(session_id)
                stopped_shells = await self._shell.stop_owned(session_id)
                self._store.end_session(session_id)
        except UnknownAgentSessionError:
            return {"session_id": session_id, "absent": True}
        return {
            "session_id": session_id,
            "absent": True,
            "stopped_jobs": stopped_jobs,
            "stopped_shells": stopped_shells,
        }

    async def _stop_owned_jobs(self, session_id: str) -> list[str]:
        """Stop executor-owned shell jobs before the session is removed."""
        listed = await self._shell.jobs.list_unlocked(
            session_id, include_finished=True
        )
        stopped: list[str] = []
        for job in listed.jobs:
            if job.status in CONFIRMED_TERMINAL_STATUSES:
                continue
            result = await self._shell.jobs.stop_unlocked(
                session_id, job.job_id
            )
            if (
                result.killed
                or result.job.status in CONFIRMED_TERMINAL_STATUSES
            ):
                stopped.append(job.job_id)
                continue
            raise RuntimeError(
                "tracked job could not be confirmed stopped: "
                f"{job.job_id}: status={result.job.status!r}"
            )
        return stopped

    async def change_cwd(self, session_id: str, workdir: str) -> Any:
        """Resolve against fixed executor root, then mutate cwd crash-safely."""
        resolved = self._resolve_workdir(workdir)
        async with session_lifecycle_lock(session_id):
            return await asyncio.to_thread(
                change_session_cwd,
                self._config,
                self._store,
                session_id,
                str(resolved),
            )

    def _resolve_workdir(self, workdir: str) -> Path:
        resolved = resolve_path_with_policy(
            workdir,
            workspace_root=self._config.workspace_root,
            allow_full_control=self._config.allow_full_control,
            path_denylist=self._config.path_denylist,
            must_exist=True,
        )
        if not resolved.is_dir():
            raise NotADirectoryError(str(resolved))
        return resolved

    def _confirm_absent_after_failed_create(self, session_id: str) -> bool:
        try:
            self._store.require_session(session_id)
        except UnknownAgentSessionError:
            return True
        try:
            self._store.end_session(session_id)
        except Exception:
            return False
        try:
            self._store.require_session(session_id)
        except UnknownAgentSessionError:
            return True
        return False

    def _summary(
        self, session_id: str, workdir: str
    ) -> SessionInventorySummary:
        last_active_at, has_shells, has_jobs = (
            self._store.session_cleanup_metadata(session_id)
        )
        return SessionInventorySummary(
            session_id=session_id,
            resolved_workdir=workdir,
            last_active_at=last_active_at,
            has_persistent_shells=has_shells,
            has_active_jobs=has_jobs,
        )
