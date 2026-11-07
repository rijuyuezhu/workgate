"""Bounded, stable job inventory pages across shell and managed backends."""

from collections.abc import Iterable
from typing import Any

import pytest

import workgate.control.jobs as control_jobs
from tests.control.test_control_jobs import FakeSessions, _job, _service
from workgate.schemas.result_models.jobs import (
    JobInfo,
    JobListOutput,
    JobOutput,
    JobTailOutput,
)


def _jobs(total: int, *, heavy: bool = False) -> list[JobInfo]:
    jobs = []
    for index in range(total):
        record = _job(f"job_{index:04d}")
        jobs.append(
            record.model_copy(
                update={
                    "created_at": float(total - index),
                    "kind": "managed" if index % 2 else "shell",
                    "status": "succeeded" if index % 3 == 0 else "running",
                    "name": "会" * 1500 if heavy else record.name,
                    "command": "长命令" * 1000 if heavy else record.command,
                    "cwd": "/路径" * 1500 if heavy else record.cwd,
                    "progress": {"payload": "进度" * 20000} if heavy else None,
                    "result": {"payload": "结果" * 20000} if heavy else None,
                    "error": "失败" * 20000 if heavy else None,
                }
            )
        )
    return jobs


def _page(
    jobs: list[JobInfo], *, limit: int = 25, offset: int = 0
) -> JobOutput:
    return control_jobs._list_page(
        jobs,
        {"running": 2, "succeeded": 1},
        limit=limit,
        offset=offset,
        message="List of tracked jobs",
    )


def _iter_pages(
    jobs: list[JobInfo], *, limit: int = 1000
) -> Iterable[JobOutput]:
    offset = 0
    for _ in range(len(jobs) + 1):
        result = _page(jobs, limit=limit, offset=offset)
        yield result
        if result.next_offset is None:
            return
        assert result.next_offset > offset
        offset = result.next_offset
    raise AssertionError("pagination did not converge")


def test_default_page_25_and_final_offset() -> None:
    rows = _jobs(53)
    first = _page(rows)
    assert len(first.jobs) == 25
    assert (first.total, first.offset, first.next_offset, first.truncated) == (
        53,
        0,
        25,
        True,
    )
    assert first.counts == {"running": 2, "succeeded": 1}
    assert [x.job_id for x in first.jobs] == [x.job_id for x in rows[:25]]
    second = _page(rows, offset=25)
    assert [x.job_id for x in second.jobs] == [x.job_id for x in rows[25:50]]
    third = _page(rows, offset=50)
    assert (
        len(third.jobs),
        third.total,
        third.next_offset,
        third.truncated,
    ) == (3, 53, None, False)
    exhausted = _page(rows, offset=1000)
    assert exhausted.jobs == []
    assert exhausted.total == 53
    assert exhausted.next_offset is None
    assert exhausted.truncated is False
    assert _page([], offset=0).jobs == []


def test_thousand_heavy_unicode_jobs_fit_serialized_budget_and_no_skips() -> (
    None
):
    rows = _jobs(1000, heavy=True)
    pages = list(_iter_pages(rows))
    assert len(pages) > 1
    ids = [row.job_id for page in pages for row in page.jobs]
    assert ids == [row.job_id for row in rows]
    assert len(ids) == len(set(ids))
    for index, page in enumerate(pages):
        assert len(page.model_dump_json().encode("utf-8")) <= 32 * 1024
        assert page.total == len(rows)
        assert page.counts == {"running": 2, "succeeded": 1}
        if index + 1 < len(pages):
            assert page.truncated and page.next_offset is not None
            assert page.next_offset == pages[index + 1].offset
        else:
            assert page.next_offset is None and page.truncated is False
        for row in page.jobs:
            assert len(row.command.encode("utf-8")) <= 240
            assert len(row.name.encode("utf-8")) <= 80
            assert len(row.cwd.encode("utf-8")) <= 160
            assert (
                row.result is None
                and row.progress is None
                and row.error is None
            )
    assert rows[0].result is not None  # Compaction must not mutate the source.
    assert len(rows[0].command) > 500


def test_pathological_control_characters_cannot_exceed_json_budget() -> None:
    rows = _jobs(1000)
    for row in rows:
        row.command = "\u0000\u001f" * 2000
        row.cwd = "\u0000" * 2000
        row.name = "\u0001" * 2000
    for page in _iter_pages(rows):
        assert len(page.model_dump_json().encode("utf-8")) <= 32 * 1024


@pytest.mark.parametrize("limit,offset", [(0, 0), (-1, 0), (1001, 0), (25, -1)])
@pytest.mark.asyncio
async def test_invalid_pagination_rejected_before_job_reads(
    limit: int, offset: int
):
    sessions = FakeSessions()
    service = _service(sessions)
    with pytest.raises(ValueError, match="limit|offset"):
        await service.execute(session_id="sess_a", limit=limit, offset=offset)
    assert not sessions.calls


@pytest.mark.asyncio
async def test_mixed_executor_managed_page_filters_and_preserves_counts(
    monkeypatch,
):
    sessions = FakeSessions()
    shell = _jobs(5)
    managed = _jobs(4)
    shell = [
        x.model_copy(
            update={
                "job_id": f"shell_{x.job_id}",
                "created_at": x.created_at * 2,
            }
        )
        for x in shell
    ]
    managed = [
        x.model_copy(
            update={
                "job_id": f"managed_{x.job_id}",
                "created_at": x.created_at * 2 + 1,
            }
        )
        for x in managed
    ]
    sessions.executor_result = JobOutput(
        operation="list", jobs=shell, counts={"running": 3, "succeeded": 2}
    )

    async def managed_list(_session: str, include_finished: bool):
        assert include_finished is True
        return JobListOutput(
            jobs=managed, counts={"running": 2, "succeeded": 2}
        )

    monkeypatch.setattr(control_jobs, "managed_job_list_execute", managed_list)
    service = _service(sessions)
    all_rows = sorted(
        [*shell, *managed], key=lambda j: j.created_at, reverse=True
    )
    observed = []
    offset = 0
    for _ in range(5):
        result = await service.execute(
            session_id="sess_a", list_jobs=True, limit=3, offset=offset
        )
        observed.extend(row.job_id for row in result.jobs)
        assert result.total == len(all_rows)
        assert result.counts == {"running": 5, "succeeded": 4}
        if result.next_offset is None:
            break
        offset = result.next_offset
    assert observed == [row.job_id for row in all_rows]


@pytest.mark.asyncio
async def test_offline_executor_fallback_managed_page_remains_bounded(
    monkeypatch,
):
    sessions = FakeSessions(availability="missing_on_executor")
    records = _jobs(100)

    async def managed_list(*_args: Any, **_kwargs: Any):
        return JobListOutput(
            jobs=records, counts={"running": 66, "succeeded": 34}
        )

    monkeypatch.setattr(control_jobs, "managed_job_list_execute", managed_list)
    service = _service(sessions)
    page = await service.execute(session_id="sess_a", limit=9, offset=18)
    assert [row.job_id for row in page.jobs] == [
        row.job_id for row in records[18:27]
    ]
    assert page.total == 100 and page.next_offset == 27
    assert "Executor jobs unavailable" in (page.message or "")
    assert sessions.calls == []


@pytest.mark.asyncio
async def test_poll_retains_full_details_and_list_does_not(monkeypatch):
    sessions = FakeSessions()
    original = _jobs(1, heavy=True)[0]
    sessions.executor_result = JobOutput(
        operation="list", jobs=[original], counts={"running": 1}
    )

    async def managed_list(*_args: Any, **_kwargs: Any):
        return JobListOutput(jobs=[], counts={})

    monkeypatch.setattr(control_jobs, "managed_job_list_execute", managed_list)
    page = await _service(sessions).execute(session_id="sess_a")
    assert page.jobs[0].job_id == original.job_id
    assert page.jobs[0].result is None
    assert original.result is not None
    assert original.error is not None

    sessions.executor_result = JobOutput(
        operation="poll",
        outputs=[JobTailOutput(job=original, output="full log")],
    )
    detail = await _service(sessions).execute(
        session_id="sess_a", poll=[original.job_id]
    )
    assert detail.outputs[0].job.command == original.command
    assert detail.outputs[0].job.result == original.result
    assert detail.outputs[0].job.error == original.error
    assert detail.outputs[0].output == "full log"


@pytest.mark.asyncio
async def test_paired_mcp_job_list_page_and_poll_full_details(
    tmp_path, monkeypatch
):
    """Real Control -> Executor path: page results and poll still operate."""
    from tests.support.paired import build_paired_mcp
    from tests.support.responses import mcp_structured, python_shell_command
    from workgate.config.settings import clear_settings_cache, get_settings

    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WORKGATE_DATA_DIR", str(tmp_path / "data"))
    clear_settings_cache()
    mcp, _harness = build_paired_mcp(get_settings())
    started = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )
    session_id = started["session_id"]
    job_ids = []
    for n in (1, 2):
        result = mcp_structured(
            await mcp.call_tool(
                "bash",
                {
                    "session_id": session_id,
                    "command": python_shell_command(f"print('marker-{n}')"),
                    "async_": True,
                },
            )
        )
        job_ids.append(result["result"]["job_id"])
    first = mcp_structured(
        await mcp.call_tool("job", {"session_id": session_id, "limit": 1})
    )
    assert first["operation"] == "list"
    assert first["total"] == 2
    assert first["offset"] == 0 and first["next_offset"] == 1
    assert first["truncated"] is True
    second = mcp_structured(
        await mcp.call_tool(
            "job", {"session_id": session_id, "limit": 1, "offset": 1}
        )
    )
    assert second["next_offset"] is None and second["truncated"] is False
    assert {first["jobs"][0]["job_id"], second["jobs"][0]["job_id"]} == set(
        job_ids
    )
    inspected = mcp_structured(
        await mcp.call_tool(
            "job", {"session_id": session_id, "poll": [job_ids[0]]}
        )
    )
    assert inspected["operation"] == "poll"
    assert inspected["outputs"][0]["job"]["job_id"] == job_ids[0]
    assert inspected["total"] is None


@pytest.mark.asyncio
async def test_include_finished_false_pages_only_active_rows_but_counts_all(
    monkeypatch,
):
    sessions = FakeSessions()
    active = _job("job_active", status="running")
    sessions.executor_result = JobOutput(
        operation="list", jobs=[active], counts={"running": 1, "succeeded": 1}
    )

    async def managed_list(_sid: str, include_finished: bool):
        assert include_finished is False
        return JobListOutput(jobs=[], counts={"failed": 4})

    monkeypatch.setattr(control_jobs, "managed_job_list_execute", managed_list)
    result = await _service(sessions).execute(
        session_id="sess_a", include_finished=False, limit=1, offset=0
    )
    assert [job.job_id for job in result.jobs] == ["job_active"]
    assert result.total == 1 and result.next_offset is None
    assert result.counts == {"running": 1, "succeeded": 1, "failed": 4}
    assert sessions.calls[0][1]["include_finished"] is False
