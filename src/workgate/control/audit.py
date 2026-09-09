"""Control-owned public audit query service."""

from __future__ import annotations

import asyncio
from typing import Any

from ..audit import current_audit_call_id, get_audit_entry, query_audit
from ..oauth.core.context import require_oauth_scopes
from ..oauth.core.scopes import SCOPE_AUDIT_FULL
from ..tools.schemas.result_models.audit import AuditTailOutput
from .sessions import ControlSessionCoordinator


class ControlAuditService:
    """Query canonical control audit history through one shared session."""

    def __init__(self, sessions: ControlSessionCoordinator) -> None:
        self._sessions = sessions

    async def execute(
        self,
        *,
        session_id: str,
        limit: int = 100,
        event: str | None = None,
        operation: str | None = None,
        audit_session: str | None = None,
        search: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
        sort: str = "desc",
        entry_id: str | None = None,
        include_full_payloads: bool = False,
    ) -> AuditTailOutput:
        if include_full_payloads and entry_id is None:
            raise ValueError("include_full_payloads requires entry_id")
        if include_full_payloads:
            require_oauth_scopes((SCOPE_AUDIT_FULL,))

        async with self._sessions.session_admission((session_id,)):
            exclude_call_id = current_audit_call_id()
            if entry_id is not None:
                entry = await asyncio.to_thread(
                    get_audit_entry,
                    entry_id,
                    include_full_payloads=include_full_payloads,
                    exclude_call_id=exclude_call_id,
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
                    session=audit_session,
                    search=search,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    sort=sort,
                    exclude_call_id=exclude_call_id,
                )

        return AuditTailOutput(
            session_id=session_id,
            entries=list(data.get("entries") or []),
            count=int(data.get("count") or 0),
            total_matched=int(data.get("total_matched") or 0),
            failed_matched=int(data.get("failed_matched") or 0),
            entry_id=entry_id,
            full_payloads=include_full_payloads,
        )
