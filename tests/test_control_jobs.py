from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import workgate.control.jobs as control_jobs
from workgate.control.jobs import ControlJobService
from workgate.schemas.result_models.jobs import (
    JobInfo,
    JobListOutput,
    JobOutput,
    JobRetryOutput,
    JobStopOutput,
)


def _job(job_id: str, *, status: str = "running") -> JobInfo:
    return JobInfo(
        job_id=job_id,
        kind="managed",
        name=job_id,
        status=status,  # type: ignore[arg-type]
        command="managed",
        cwd=".",
        session_id="sess_a",
        created_at=1.0,
        updated_at=1.0,
        attempts=1,
    )


class FakeSessions:
    def __init__(
        self, *, status: str = "active", availability: str = "available"
    ):
        self.status = status
        self.availability = availability
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.admitted: list[tuple[str, ...]] = []
        self.executor_result = JobOutput(operation="list")

    def require_session_status(self, session_id: str, allowed: set[str]):
        assert session_id == "sess_a"
        assert self.status in allowed
        return SimpleNamespace(status=self.status)

    async def session_availability(self, session_id: str) -> str:
        assert session_id == "sess_a"
        return self.availability

    async def call_session_tool(self, tool: str, args: dict[str, Any]):
        self.calls.append((tool, args))
        return self.executor_result.model_dump(mode="json")

    @asynccontextmanager
    async def session_admission(self, session_ids: tuple[str, ...]):
        self.admitted.append(session_ids)
        yield ()


@pytest.mark.asyncio
async def test_control_job_rejects_conflicting_actions() -> None:
    service = ControlJobService(FakeSessions())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="list_jobs cannot be combined"):
        await service.execute(
            session_id="sess_a", list_jobs=True, poll=["job_a"]
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        await service.execute(
            session_id="sess_a",
            poll=["job_a"],
            cancel=["job_b"],
        )


@pytest.mark.asyncio
async def test_control_job_list_merges_executor_and_managed_rows(
    monkeypatch,
) -> None:
    sessions = FakeSessions()
    executor_job = _job("job_executor")
    managed_job = _job("job_managed")
    sessions.executor_result = JobOutput(
        operation="list",
        jobs=[executor_job],
        counts={"running": 1},
    )

    async def managed_list(*_args: Any, **_kwargs: Any):
        return JobListOutput(jobs=[managed_job], counts={"running": 1})

    monkeypatch.setattr(control_jobs, "managed_job_list_execute", managed_list)
    service = ControlJobService(sessions)  # type: ignore[arg-type]

    result = await service.execute(
        session_id="sess_a", list_jobs=True, lines=17
    )

    assert result.operation == "list"
    assert [job.job_id for job in result.jobs] == [
        "job_executor",
        "job_managed",
    ]
    assert result.counts == {"running": 2}
    assert sessions.calls[0][0] == "job"
    assert sessions.calls[0][1]["lines"] == 17


@pytest.mark.asyncio
async def test_control_job_list_keeps_managed_rows_when_executor_unavailable(
    monkeypatch,
) -> None:
    sessions = FakeSessions(availability="missing_on_executor")
    managed_job = _job("job_managed")

    async def managed_list(*_args: Any, **_kwargs: Any):
        return JobListOutput(jobs=[managed_job], counts={"running": 1})

    monkeypatch.setattr(control_jobs, "managed_job_list_execute", managed_list)
    service = ControlJobService(sessions)  # type: ignore[arg-type]

    result = await service.execute(session_id="sess_a")

    assert [job.job_id for job in result.jobs] == ["job_managed"]
    assert "Executor jobs unavailable" in (result.message or "")
    assert sessions.calls == []


@pytest.mark.asyncio
async def test_control_job_cancel_preserves_requested_managed_executor_order(
    monkeypatch,
) -> None:
    sessions = FakeSessions()
    managed_stop = JobStopOutput(job=_job("job_managed"), killed=True)
    executor_stop = JobStopOutput(job=_job("job_executor"), killed=True)
    sessions.executor_result = JobOutput(
        operation="cancel",
        cancelled=[executor_stop],
    )

    monkeypatch.setattr(
        control_jobs,
        "managed_job_id_set",
        lambda _session_id, _requested: {"job_managed"},
    )

    async def stop_managed(_session_id: str, job_id: str):
        assert job_id == "job_managed"
        return managed_stop

    monkeypatch.setattr(
        control_jobs,
        "stop_managed_job_without_session_admission",
        stop_managed,
    )
    service = ControlJobService(sessions)  # type: ignore[arg-type]

    result = await service.execute(
        session_id="sess_a",
        cancel=["job_executor", "job_managed"],
    )

    assert [row.job.job_id for row in result.cancelled] == [
        "job_executor",
        "job_managed",
    ]
    assert sessions.calls[0][1]["cancel"] == ["job_executor"]


@pytest.mark.asyncio
async def test_control_job_retry_admits_referenced_sessions(
    monkeypatch,
) -> None:
    sessions = FakeSessions()
    retried = JobRetryOutput(
        **_job("job_managed", status="running").model_dump()
    )
    monkeypatch.setattr(
        control_jobs,
        "managed_job_id_set",
        lambda _session_id, _requested: {"job_managed"},
    )
    monkeypatch.setattr(
        control_jobs,
        "managed_job_referenced_session_ids",
        lambda _session_id, _job_id: ("sess_a", "sess_b"),
    )

    async def retry_managed(_session_id: str, job_id: str):
        assert job_id == "job_managed"
        return retried

    monkeypatch.setattr(
        control_jobs,
        "retry_managed_job_without_session_admission",
        retry_managed,
    )
    service = ControlJobService(sessions)  # type: ignore[arg-type]

    result = await service.execute(session_id="sess_a", retry=["job_managed"])

    assert [row.job_id for row in result.retried] == ["job_managed"]
    assert sessions.admitted == [("sess_a", "sess_b")]


@pytest.mark.asyncio
async def test_control_job_cleanup_hooks_use_both_session_copy_references(
    monkeypatch,
) -> None:
    sessions = FakeSessions()
    reference_keys: list[str] = []

    monkeypatch.setattr(
        control_jobs,
        "managed_job_has_active_reference",
        lambda session_id, **kwargs: (
            session_id == "sess_a"
            and kwargs["payload_keys"] == ("src_session_id", "dst_session_id")
        ),
    )

    async def stop_refs(_session_id: str, **kwargs: Any):
        reference_keys.append(kwargs["payload_key"])
        return ["job_same"]

    monkeypatch.setattr(
        control_jobs,
        "job_stop_managed_references_execute",
        stop_refs,
    )
    service = ControlJobService(sessions)  # type: ignore[arg-type]

    assert await service.auto_cleanup_blocked("sess_a") is True
    assert await service.stop_referencing_jobs("sess_a") == ["job_same"]
    assert reference_keys == ["src_session_id", "dst_session_id"]
