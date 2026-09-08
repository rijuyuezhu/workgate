"""Control-side hybrid routing for executor jobs and managed orchestration jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..jobs.managed import (
    job_stop_managed_references_execute,
    managed_job_has_active_reference,
    managed_job_id_set,
    managed_job_list_execute,
    managed_job_referenced_session_ids,
    managed_job_tail_execute,
    retry_managed_job_without_session_admission,
    stop_managed_job_without_session_admission,
)
from ..schemas.result_models.jobs import JobOutput
from .session_copy import SESSION_COPY_MANAGED_KIND
from .sessions import ControlSessionCoordinator


def _merge_counts(*counts: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for source in counts:
        for status, value in source.items():
            merged[status] = merged.get(status, 0) + int(value)
    return merged


def _ordered_rows(
    requested: list[str],
    local_rows: list[Any],
    executor_rows: list[Any],
    *,
    job_id: Callable[[Any], str],
) -> list[Any]:
    by_id = {job_id(row): row for row in executor_rows}
    by_id.update({job_id(row): row for row in local_rows})
    return [by_id[item] for item in requested if item in by_id]


class ControlJobService:
    """Merge control-managed jobs with executor-owned background resources."""

    def __init__(self, sessions: ControlSessionCoordinator) -> None:
        self._sessions = sessions

    async def execute(
        self,
        *,
        session_id: str,
        list_jobs: bool = False,
        poll: list[str] | None = None,
        cancel: list[str] | None = None,
        retry: list[str] | None = None,
        include_finished: bool = True,
        lines: int = 200,
    ) -> JobOutput:
        selected = [poll is not None, cancel is not None, retry is not None]
        if list_jobs and any(selected):
            raise ValueError(
                "list_jobs cannot be combined with poll, cancel, or retry"
            )
        if sum(selected) > 1:
            raise ValueError("poll, cancel, and retry are mutually exclusive")

        record = self._sessions.require_session_status(
            session_id, {"active", "missing", "terminating"}
        )
        if list_jobs or not any(selected):
            return await self._list(
                session_id,
                include_finished=include_finished,
                executor_available=record.status == "active",
                lines=lines,
            )

        requested = list(poll or cancel or retry or [])
        managed_ids = managed_job_id_set(session_id, requested)
        local_ids = [item for item in requested if item in managed_ids]
        executor_ids = [item for item in requested if item not in managed_ids]
        executor = JobOutput(
            operation=(
                "poll"
                if poll is not None
                else "cancel"
                if cancel is not None
                else "retry"
            )
        )
        if executor_ids:
            executor = JobOutput.model_validate(
                await self._sessions.call_session_tool(
                    "job",
                    {
                        "session_id": session_id,
                        "poll": executor_ids if poll is not None else None,
                        "cancel": executor_ids if cancel is not None else None,
                        "retry": executor_ids if retry is not None else None,
                        "include_finished": include_finished,
                        "lines": lines,
                    },
                )
            )

        if poll is not None:
            local_rows = [
                await managed_job_tail_execute(session_id, item, lines)
                for item in local_ids
            ]
            return JobOutput(
                operation="poll",
                outputs=_ordered_rows(
                    requested,
                    local_rows,
                    executor.outputs,
                    job_id=lambda row: row.job.job_id,
                ),
            )
        if cancel is not None:
            local_rows = [
                await stop_managed_job_without_session_admission(
                    session_id, item
                )
                for item in local_ids
            ]
            return JobOutput(
                operation="cancel",
                cancelled=_ordered_rows(
                    requested,
                    local_rows,
                    executor.cancelled,
                    job_id=lambda row: row.job.job_id,
                ),
            )

        local_rows = []
        for item in local_ids:
            session_ids = managed_job_referenced_session_ids(session_id, item)
            async with self._sessions.session_admission(session_ids):
                local_rows.append(
                    await retry_managed_job_without_session_admission(
                        session_id, item
                    )
                )
        return JobOutput(
            operation="retry",
            retried=_ordered_rows(
                requested,
                local_rows,
                executor.retried,
                job_id=lambda row: row.job_id,
            ),
        )

    async def auto_cleanup_blocked(self, session_id: str) -> bool:
        """Protect sessions referenced by live control-managed copy work."""
        return await asyncio.to_thread(
            managed_job_has_active_reference,
            session_id,
            managed_kind=SESSION_COPY_MANAGED_KIND,
            payload_keys=("src_session_id", "dst_session_id"),
        )

    async def stop_referencing_jobs(self, session_id: str) -> list[str]:
        """Cancel live managed copies before their referenced session disappears."""
        stopped: list[str] = []
        for payload_key in ("src_session_id", "dst_session_id"):
            stopped.extend(
                await job_stop_managed_references_execute(
                    session_id,
                    managed_kind=SESSION_COPY_MANAGED_KIND,
                    payload_key=payload_key,
                )
            )
        return list(dict.fromkeys(stopped))

    async def _list(
        self,
        session_id: str,
        *,
        include_finished: bool,
        executor_available: bool,
        lines: int,
    ) -> JobOutput:
        managed = await managed_job_list_execute(
            session_id, include_finished, touch_session=False
        )
        if not executor_available:
            if managed.jobs:
                return JobOutput(
                    operation="list",
                    jobs=managed.jobs,
                    counts=managed.counts,
                    message="Executor jobs unavailable while the session is not active.",
                )
            raise RuntimeError(
                "executor jobs unavailable while the session is not active"
            )
        try:
            executor = JobOutput.model_validate(
                await self._sessions.call_session_tool(
                    "job",
                    {
                        "session_id": session_id,
                        "list_jobs": True,
                        "poll": None,
                        "cancel": None,
                        "retry": None,
                        "include_finished": include_finished,
                        "lines": lines,
                    },
                )
            )
        except Exception as exc:
            if not managed.jobs:
                raise
            return JobOutput(
                operation="list",
                jobs=managed.jobs,
                counts=managed.counts,
                message=(
                    f"Executor jobs unavailable: {type(exc).__name__}: {exc}"
                ),
            )
        jobs = [*executor.jobs, *managed.jobs]
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return JobOutput(
            operation="list",
            jobs=jobs,
            counts=_merge_counts(executor.counts, managed.counts),
            message=(
                "No tracked jobs in this session."
                if not jobs
                else "Tracked executor and control-managed job snapshot for this session."
            ),
        )
