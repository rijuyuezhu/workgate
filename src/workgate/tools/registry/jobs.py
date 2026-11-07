"""Tracked shell and managed job companion tool registry."""

from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.jobs import JobOutput
from ..declarative import DeclarativeToolRegistry
from ..schemas.input_models.jobs import (
    IncludeFinishedArg,
    JobCancelIdsArg,
    JobListLimitArg,
    JobListOffsetArg,
    JobListSnapshotArg,
    JobPollIdsArg,
    JobRetryIdsArg,
    JobTailLinesArg,
)


class JobToolRegistry(DeclarativeToolRegistry):
    """Register tracked shell and managed job companion tools."""

    name = "jobs"
    """Registry group name used for tool-surface organization."""


job_tool = JobToolRegistry.get_tool_decorator()


def _job_description(_context: object) -> str:
    return """List compact, paginated Job summaries, or poll, cancel and retry Jobs by session_id and job_id. Use limit/offset for list pages and next_offset for continuation; poll a job_id for its full metadata and log. Includes durable bash(async_=true) and managed session_copy jobs; use only one action per call. A lost managed job may restart via retry if its owning sessions remain available."""


@job_tool(
    description=_job_description,
    oauth_scopes=("shell:read", "shell:execute"),
)
async def job(
    session_id: SessionIdArg,
    list_jobs: JobListSnapshotArg = False,
    poll: JobPollIdsArg = None,
    cancel: JobCancelIdsArg = None,
    retry: JobRetryIdsArg = None,
    include_finished: IncludeFinishedArg = True,
    lines: JobTailLinesArg = 200,
    limit: JobListLimitArg = 25,
    offset: JobListOffsetArg = 0,
) -> JobOutput:
    """Declare the public job signature; control composition owns execution."""
    del (
        session_id,
        list_jobs,
        poll,
        cancel,
        retry,
        include_finished,
        lines,
        limit,
        offset,
    )
    raise RuntimeError("job tool requires control routing")
