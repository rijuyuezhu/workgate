"""Control-owned public audit query service."""

import asyncio
from typing import Any

from ..audit import current_audit_call_id, get_audit_entry, query_audit
from ..oauth.core.context import require_oauth_scopes
from ..oauth.core.scopes import SCOPE_AUDIT_FULL
from ..tools.schemas.result_models.audit import AuditTailOutput
from .state import ControlState
from .task_state import ControlTaskService


class ControlAuditService:
    """Query canonical audit history by semantic task and/or execution session."""

    def __init__(self, tasks: ControlTaskService, state: ControlState) -> None:
        self._tasks = tasks
        self._state = state

    async def execute(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        event: str | None = None,
        operation: str | None = None,
        search: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
        sort: str = "desc",
        entry_id: str | None = None,
        include_full_payloads: bool = False,
    ) -> AuditTailOutput:
        if task_id is None and session_id is None:
            raise ValueError("task_id or session_id is required")
        if include_full_payloads and entry_id is None:
            raise ValueError("include_full_payloads requires entry_id")
        if include_full_payloads:
            require_oauth_scopes((SCOPE_AUDIT_FULL,))

        attached_sessions: set[str] = set()
        if task_id is not None:
            task = await self._tasks.read_task(task_id)
            attached_sessions = set(task.session_ids)
        if session_id is not None:
            if session_id not in self._state.snapshot_sessions():
                raise ValueError(f"unknown session_id {session_id!r}")
            if task_id is not None and session_id not in attached_sessions:
                raise ValueError(
                    f"session {session_id} is not attached to task {task_id}"
                )

        exclude_call_id = current_audit_call_id()
        if entry_id is not None:
            entry = await asyncio.to_thread(
                get_audit_entry,
                entry_id,
                include_full_payloads=include_full_payloads,
                exclude_call_id=exclude_call_id,
            )
            entry_tasks = {
                str(value)
                for value in entry.get("task_ids") or []
                if isinstance(value, str) and value
            }
            if isinstance(entry.get("task"), str) and entry.get("task"):
                entry_tasks.add(str(entry["task"]))
            entry_sessions = {
                str(value)
                for value in entry.get("session_ids") or []
                if isinstance(value, str) and value
            }
            if isinstance(entry.get("session"), str) and entry.get("session"):
                entry_sessions.add(str(entry["session"]))
            if (
                task_id is not None
                and task_id not in entry_tasks
                and not (not entry_tasks and entry_sessions & attached_sessions)
            ):
                raise ValueError(
                    f"Unknown audit entry for task {task_id}: {entry_id}"
                )
            if session_id is not None and session_id not in entry_sessions:
                raise ValueError(
                    f"Audit entry {entry_id} does not belong to session {session_id}"
                )
            failed = int(
                entry.get("ok") is False
                or str(entry.get("status") or "") == "failed"
            )
            data: dict[str, Any] = {
                "entries": [entry],
                "count": 1,
                "total_matched": 1,
                "failed_matched": failed,
            }
        else:
            data = await asyncio.to_thread(
                query_audit,
                limit=limit,
                event=event,
                operation=operation,
                session=session_id,
                task=task_id,
                search=search,
                start_ts=start_ts,
                end_ts=end_ts,
                sort=sort,
                exclude_call_id=exclude_call_id,
            )

        return AuditTailOutput(
            task_id=task_id,
            session_id=session_id,
            entries=list(data.get("entries") or []),
            count=int(data.get("count") or 0),
            total_matched=int(data.get("total_matched") or 0),
            failed_matched=int(data.get("failed_matched") or 0),
            entry_id=entry_id,
            full_payloads=include_full_payloads,
        )
