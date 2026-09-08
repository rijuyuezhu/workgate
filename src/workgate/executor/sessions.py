"""Executor-authoritative shared session resources for protocol v1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..ops.session import (
    _session_output,
    session_change_cwd_execute,
    session_end_execute,
)
from ..ops.utils.path import resolve_path_with_policy
from ..protocol.executor import SessionInventorySummary
from ..tool_session.store import ToolSessionStore, UnknownAgentSessionError
from .config import ExecutorConfig
from .errors import ExecutorOperationFailure


class ExecutorSessionService:
    """Own final shared session IDs and their executor-side durable state."""

    def __init__(self, config: ExecutorConfig, store: ToolSessionStore) -> None:
        self._config = config
        self._store = store

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
                target="local",
                workdir=resolved,
                label=label,
            )
            return _session_output(session)
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
            ended = await session_end_execute(session_id)
        except UnknownAgentSessionError:
            return {"session_id": session_id, "absent": True}
        return {
            "session_id": session_id,
            "absent": True,
            "stopped_jobs": ended.stopped_jobs,
            "stopped_shells": ended.stopped_shells,
        }

    async def change_cwd(self, session_id: str, workdir: str) -> Any:
        """Resolve against fixed executor root, then mutate cwd crash-safely."""
        resolved = self._resolve_workdir(workdir)
        return await session_change_cwd_execute(session_id, str(resolved))

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
