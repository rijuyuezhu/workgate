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
    return """Inspect durable output, structured progress/results, stop, or retry tracked work owned by an explicit agent session.

`bash(async_=true)` creates tracked shell jobs. `session_copy(background=true)` creates controller-managed transfer jobs owned by the source session. Both return job_id values managed through this companion. Shell exit state and bounded logs remain durable after completion and server restart; managed jobs additionally retain bounded progress, durable payload, and structured result, while their live task remains process-local. If a controller-managed task disappears after process loss, it becomes `lost` and may be retried from its stored payload while the owning sessions remain available. Starting work belongs in `bash` or `session_copy`, not `job`.

Pass the session_id from session_start. Call with no action, or with `list_jobs=true`, to list jobs owned by that session. Use `poll=[id]` to inspect output, progress, result, and status; `cancel=[id]` to stop work and trigger transactional cleanup; or `retry=[id]` to restart from the original shell command or managed payload. Lists merge executor-owned shell jobs with control-managed jobs, and control-managed jobs remain inspectable when the executor is offline. Do not combine actions in one call."""


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
