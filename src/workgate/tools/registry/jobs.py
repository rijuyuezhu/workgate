"""Tracked shell and managed job companion tool registry."""

from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.jobs import JobOutput
from ..declarative import DeclarativeToolRegistry
from ..schemas.input_models.jobs import (
    IncludeFinishedArg,
    JobCancelIdsArg,
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
    return """List, poll, cancel, or retry durable jobs by session_id and job_id. Includes bash(async_=true) and managed session_copy jobs; use only one action per call. A lost managed job may restart via retry if its owning sessions remain available."""


@job_tool(
    http_method="POST",
    http_path="/tools/job",
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
) -> JobOutput:
    """Declare the public job signature; control composition owns execution."""
    del session_id, list_jobs, poll, cancel, retry, include_finished, lines
    raise RuntimeError("job tool requires control routing")
