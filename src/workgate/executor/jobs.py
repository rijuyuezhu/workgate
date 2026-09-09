"""Executor-owned shell-job orchestration."""

from __future__ import annotations

from typing import Any

from ..jobs import shell as shell_jobs
from ..schemas.result_models.jobs import (
    JobListOutput,
    JobOutput,
    JobRetryOutput,
    JobStartOutput,
    JobStopOutput,
    JobTailOutput,
)
from ..tool_session.lifecycle import session_lifecycle_lock
from ..tool_session.store import ToolSessionStore
from .config import ExecutorConfig


class ExecutorJobService:
    """Own shell-backed tracked jobs for one executor runtime."""

    def __init__(self, config: ExecutorConfig, store: ToolSessionStore) -> None:
        self.config = config
        self.store = store

    async def start(
        self,
        session_id: str,
        command: str,
        cwd: str = ".",
        name: str | None = None,
    ) -> JobStartOutput:
        async with session_lifecycle_lock(session_id):
            return await self.start_unlocked(
                session_id, command, cwd=cwd, name=name
            )

    async def start_unlocked(
        self,
        session_id: str,
        command: str,
        cwd: str = ".",
        name: str | None = None,
    ) -> JobStartOutput:
        return await shell_jobs.start_shell_job_unlocked(
            self.config,
            self.store,
            session_id,
            command,
            cwd=cwd,
            name=name,
        )

    async def list(
        self, session_id: str, include_finished: bool = True
    ) -> JobListOutput:
        async with session_lifecycle_lock(session_id):
            return await self.list_unlocked(session_id, include_finished)

    async def list_unlocked(
        self, session_id: str, include_finished: bool = True
    ) -> JobListOutput:
        return await shell_jobs.list_shell_jobs_unlocked(
            self.config,
            self.store,
            session_id,
            include_finished=include_finished,
        )

    async def tail(
        self, session_id: str, job_id: str, lines: int = 200
    ) -> JobTailOutput:
        async with session_lifecycle_lock(session_id):
            return await shell_jobs.tail_shell_job_unlocked(
                self.config,
                self.store,
                session_id,
                job_id,
                lines,
            )

    async def stop(self, session_id: str, job_id: str) -> JobStopOutput:
        async with session_lifecycle_lock(session_id):
            return await self.stop_unlocked(session_id, job_id)

    async def stop_unlocked(
        self, session_id: str, job_id: str
    ) -> JobStopOutput:
        return await shell_jobs.stop_shell_job_unlocked(
            self.config, self.store, session_id, job_id
        )

    async def retry(self, session_id: str, job_id: str) -> JobRetryOutput:
        async with session_lifecycle_lock(session_id):
            return await shell_jobs.retry_shell_job_unlocked(
                self.config, self.store, session_id, job_id
            )

    async def reconcile(self) -> bool:
        return await shell_jobs.reconcile_shell_jobs_execute(
            self.config, self.store
        )

    async def execute(self, args: dict[str, Any]) -> JobOutput:
        """Execute one public executor-side shell-job companion operation."""
        session_id = str(args["session_id"])
        list_jobs = bool(args.get("list_jobs", False))
        poll = _optional_string_list(args.get("poll"))
        cancel = _optional_string_list(args.get("cancel"))
        retry = _optional_string_list(args.get("retry"))
        include_finished = bool(args.get("include_finished", True))
        lines = int(args.get("lines") or 200)

        selected = [poll is not None, cancel is not None, retry is not None]
        if list_jobs and any(selected):
            raise ValueError(
                "list_jobs cannot be combined with poll, cancel, or retry"
            )
        if sum(selected) > 1:
            raise ValueError("poll, cancel, and retry are mutually exclusive")

        if list_jobs or not any(selected):
            result = await self.list(session_id, include_finished)
            return JobOutput(
                operation="list",
                jobs=result.jobs,
                counts=result.counts,
                message=(
                    "No tracked jobs in this session."
                    if not result.jobs
                    else "Tracked job snapshot for this session."
                ),
            )

        if poll is not None:
            outputs = [
                await self.tail(session_id, job_id, lines) for job_id in poll
            ]
            return JobOutput(operation="poll", outputs=outputs)

        if cancel is not None:
            cancelled = [
                await self.stop(session_id, job_id) for job_id in cancel
            ]
            return JobOutput(operation="cancel", cancelled=cancelled)

        retried = [
            await self.retry(session_id, job_id) for job_id in retry or []
        ]
        return JobOutput(operation="retry", retried=retried)


def _optional_string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("job id collection must be a list")
    return [str(item) for item in value]
