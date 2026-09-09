from __future__ import annotations

import pytest

from tests.helpers import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()


@pytest.mark.asyncio
async def test_shared_session_start_binds_control_record_to_executor(
    tmp_path, monkeypatch
):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "project").mkdir()
    harness = build_paired_control_harness(get_settings())

    started = await harness.control.session_coordinator.start_session(
        workdir="project", label="test", executor_id=harness.executor_id
    )

    assert isinstance(started, dict)
    session_id = str(started["session_id"])
    assert started["executor_id"] == harness.executor_id
    record = harness.control.control_state.snapshot_sessions()[session_id]
    assert record.status == "active"
    assert record.executor_id == harness.executor_id
    assert record.label == "test"
    assert record.resolved_workdir_display == str(tmp_path / "project")
    assert harness.executor.sessions.lookup(session_id) is not None


@pytest.mark.asyncio
async def test_shared_session_change_cwd_updates_control_and_executor_binding(
    tmp_path, monkeypatch
):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    harness = build_paired_control_harness(get_settings())
    started = await harness.control.session_coordinator.start_session(
        workdir="first", executor_id=harness.executor_id
    )
    assert isinstance(started, dict)
    session_id = str(started["session_id"])

    changed = await harness.control.session_coordinator.change_cwd(
        session_id, "second"
    )

    assert isinstance(changed, dict)
    assert changed["session_id"] == session_id
    assert changed["executor_id"] == harness.executor_id
    assert changed["workdir"] == str(tmp_path / "second")
    record = harness.control.control_state.snapshot_sessions()[session_id]
    assert record.requested_workdir == "second"
    assert record.resolved_workdir_display == str(tmp_path / "second")
    executor_session = harness.executor.sessions.lookup(session_id)
    assert executor_session is not None
    assert executor_session.resolved_workdir == str(tmp_path / "second")


@pytest.mark.asyncio
async def test_shared_session_end_requires_executor_absence_before_marking_ended(
    tmp_path, monkeypatch
):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "project").mkdir()
    harness = build_paired_control_harness(get_settings())
    started = await harness.control.session_coordinator.start_session(
        workdir="project", executor_id=harness.executor_id
    )
    assert isinstance(started, dict)
    session_id = str(started["session_id"])

    ended = await harness.control.session_coordinator.end_session(session_id)

    assert ended == {
        "session_id": session_id,
        "executor_id": harness.executor_id,
        "ended": True,
        "force_released": False,
        "stopped_jobs": [],
        "stopped_shells": [],
    }
    record = harness.control.control_state.snapshot_sessions()[session_id]
    assert record.status == "ended"
    assert all(
        item.session_id != session_id
        for item in harness.executor.sessions.inventory()
    )


@pytest.mark.asyncio
async def test_ended_shared_session_cannot_be_reused(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "project").mkdir()
    harness = build_paired_control_harness(get_settings())
    started = await harness.control.session_coordinator.start_session(
        workdir="project", executor_id=harness.executor_id
    )
    assert isinstance(started, dict)
    session_id = str(started["session_id"])
    await harness.control.session_coordinator.end_session(session_id)

    with pytest.raises(ValueError, match="operation requires"):
        await harness.control.session_coordinator.call_session_tool(
            "read", {"session_id": session_id, "path": "missing.txt"}
        )
