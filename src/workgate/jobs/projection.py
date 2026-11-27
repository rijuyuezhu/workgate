"""Shared bounded Job metadata projection."""

from ..schemas.result_models.jobs import JobInfo


def _preview(text: str, maximum_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= maximum_bytes:
        return text
    return raw[: maximum_bytes - 3].decode("utf-8", errors="ignore") + "…"


def compact_job(job: JobInfo) -> JobInfo:
    """Keep list entries useful without duplicating full Job details from poll."""
    return job.model_copy(
        update={
            "name": _preview(job.name, 80),
            "command": _preview(job.command, 240),
            "cwd": _preview(job.cwd, 160),
            "progress": None,
            "result": None,
            "error": None,
        }
    )
