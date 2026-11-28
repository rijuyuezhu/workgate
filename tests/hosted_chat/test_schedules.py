from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.schedule_cadence import (
    next_run,
    validate_cadence_interval,
)
from workgate.hosted_chat.schedules import ScheduleConflict, ScheduleRepository
from workgate.hosted_chat.storage import WorkspaceRepository


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


@pytest.mark.parametrize(
    "cadence,zone,start,expected",
    [
        (
            {"frequency": "hourly", "hour": 0, "minute": 15},
            "UTC",
            "2026-11-28T10:12:00+00:00",
            "2026-11-28T11:15:00+00:00",
        ),
        (
            {"frequency": "daily", "hour": 9, "minute": 0},
            "Asia/Shanghai",
            "2026-11-28T00:00:00+00:00",
            "2026-11-28T01:00:00+00:00",
        ),
        (
            {"frequency": "weekdays", "hour": 9, "minute": 0},
            "UTC",
            "2026-11-28T00:00:00+00:00",
            "2026-11-30T09:00:00+00:00",
        ),
        (
            {"frequency": "weekly", "hour": 9, "minute": 0, "daysOfWeek": [0]},
            "UTC",
            "2026-11-28T00:00:00+00:00",
            "2026-11-29T09:00:00+00:00",
        ),
        (
            {"frequency": "cron", "expression": "0 9 29 2 *"},
            "UTC",
            "2028-03-01T00:00:00+00:00",
            "2032-02-29T09:00:00+00:00",
        ),
        (
            {"frequency": "cron", "expression": "30 2 * * *"},
            "America/New_York",
            "2026-03-08T00:00:00+00:00",
            "2026-03-09T06:30:00+00:00",
        ),
        (
            {"frequency": "cron", "expression": "30 1 * * *"},
            "America/New_York",
            "2026-11-01T05:30:00+00:00",
            "2026-11-01T06:30:00+00:00",
        ),
        (
            {"frequency": "cron", "expression": "0 9 1 * 1"},
            "UTC",
            "2026-11-28T00:00:00+00:00",
            "2026-11-30T09:00:00+00:00",
        ),
    ],
)
def test_next_run(cadence, zone, start, expected):
    assert next_run(cadence, zone, after_ms=ms(start)) == ms(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "* * * * *",
        "*/2 * * * *",
        "0,59 0,23 * * *",
        "0 0 31 2 *",
        "0 0 * *",
        "*/bad * * * *",
        "*/0 * * * *",
        "60 0 * * *",
        "0 0 0 * *",
        "0 0 * * 8",
        "0 0 * * 1/",
        "0 0 * * 1,,2",
    ],
)
def test_invalid_cron(expression):
    cadence = {"frequency": "cron", "expression": expression}
    with pytest.raises(ValueError):
        validate_cadence_interval(cadence)
        next_run(cadence, "UTC", after_ms=ms("2026-11-28T00:00:00+00:00"))


def test_sparse_weekday_boundary():
    validate_cadence_interval(
        {"frequency": "cron", "expression": "0,59 0,23 * * 1"}
    )
    assert next_run(
        {"frequency": "cron", "expression": "*/5 * * * *"},
        "UTC",
        after_ms=ms("2026-11-28T10:05:00+00:00"),
        min_interval_minutes=0,
    ) == ms("2026-11-28T10:10:00+00:00")


@pytest.fixture
def state(tmp_path):
    db = SQLiteDatabase(tmp_path)
    aid = AgentRepository(db).create(
        "alice", {"name": "Research", "provider": "openAI", "model": "test"}
    )["id"]
    return (
        db,
        ScheduleRepository(db),
        {
            "name": "Daily research",
            "prompt": "Review research",
            "agent_id": aid,
            "timezone": "UTC",
            "cadence": {"frequency": "daily", "hour": 9, "minute": 0},
            "clientRequestId": "first",
        },
    )


def test_crud_revision_and_preserve_due(state):
    db, repo, payload = state
    first = repo.create("alice", payload)
    sid = first["id"]
    assert first["enabled"] and first["nextRunAt"]
    assert repo.create("alice", payload)["id"] == sid
    assert repo.list("bob") == []
    with pytest.raises(KeyError):
        repo.get("bob", sid)
    with pytest.raises(KeyError):
        repo.update("bob", sid, {"enabled": False})
    with pytest.raises(KeyError):
        repo.delete("bob", sid)
    with db.connect() as conn:
        conn.execute(
            "UPDATE webui_schedules SET next_run=? WHERE id=?",
            (ms("2030-01-01T09:00:00+00:00"), sid),
        )
    updated = repo.update(
        "alice", sid, {"name": "Renamed", "expectedConfigRevision": 0}
    )
    assert updated["nextRunAt"] == "2030-01-01T09:00:00.000Z"
    assert updated["configRevision"] == 1
    with pytest.raises(ScheduleConflict):
        repo.update("alice", sid, {"expectedConfigRevision": 0})
    paused = repo.update("alice", sid, {"enabled": False})
    assert paused["nextRunAt"] is None
    resumed = repo.update("alice", sid, {"enabled": True})
    assert resumed["nextRunAt"] != updated["nextRunAt"]
    repo.delete("alice", sid)
    assert not repo.list("alice")


def test_dependency_ownership_and_pause(state):
    db, repo, payload = state
    with pytest.raises(KeyError):
        repo.create("bob", payload)
    with pytest.raises(KeyError):
        repo.create("alice", {**payload, "chatProjectId": "missing"})
    with pytest.raises(KeyError):
        repo.create("alice", {**payload, "file_ids": ["missing"]})
    project = WorkspaceRepository(db).create_project("alice", "Research")
    first = repo.create("alice", {**payload, "chatProjectId": project["id"]})
    AgentRepository(db).delete("alice", payload["agent_id"])
    assert (
        repo.update("alice", first["id"], {"enabled": False})["enabled"]
        is False
    )
    with pytest.raises(KeyError):
        repo.update("alice", first["id"], {"enabled": True})


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": 1},
        {"name": ""},
        {"timezone": "Bogus/Zone"},
        {"cadence": {"frequency": "daily", "hour": 24, "minute": 0}},
        {"target": "existing"},
        {"file_ids": ["a", "a"]},
        {"clientRequestId": ""},
        {"extra": 1},
    ],
)
def test_invalid_payload(state, patch):
    _, repo, payload = state
    with pytest.raises(ValueError):
        repo.create("alice", {**payload, **patch})


def test_concurrent_idempotency_and_quota(state, monkeypatch):
    _, repo, payload = state
    from workgate.hosted_chat.schedules import SCHEDULE_LIMITS

    monkeypatch.setitem(SCHEDULE_LIMITS, "maxPerUser", 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(
            pool.map(lambda _: repo.create("alice", payload)["id"], range(4))
        )
    assert len(set(ids)) == 1
    with pytest.raises(ValueError, match="limit"):
        repo.create("alice", {**payload, "clientRequestId": "second"})
