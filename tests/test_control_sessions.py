from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from workgate.control.sessions import ControlSessionCoordinator
from workgate.control.state import (
    ControlSessionRecord,
    ControlState,
    ExecutorTrustRecord,
)
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.executor import (
    EXECUTOR_CAPABILITY_SESSIONS,
    ExecutorHelloRequest,
    ExecutorResult,
    ExecutorRuntimeSummary,
    SessionInventorySummary,
)
from workgate.protocol.ids import (
    new_command_id,
    new_executor_id,
    new_session_id,
)


def _state(tmp_path: Path) -> ControlState:
    state = ControlState(FileStateStore(lambda: tmp_path / "state"))
    state.start()
    return state


def _trust(state: ControlState, executor_id: str) -> None:
    credential = new_executor_credential()
    state.put_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="executor",
            credential_verifier=executor_credential_verifier(credential),
            created_at=1,
        )
    )


def _hello(*sessions: tuple[str, str]) -> ExecutorHelloRequest:
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        capabilities=(EXECUTOR_CAPABILITY_SESSIONS,),
        workspace_root="/workspace",
        sessions=tuple(
            SessionInventorySummary(
                session_id=session_id, resolved_workdir=workdir
            )
            for session_id, workdir in sessions
        ),
        shells=(),
        jobs=(),
    )


def _ok(result: Any) -> ExecutorResult:
    return ExecutorResult(id=new_command_id(), ok=True, result=result)


class FakeTransport:
    def __init__(self) -> None:
        self.online: set[str] = set()
        self.hellos: dict[str, ExecutorHelloRequest] = {}
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.call_impl: (
            Callable[
                [str, str, dict[str, Any], str | None, float | None],
                Awaitable[ExecutorResult],
            ]
            | None
        ) = None

    async def is_online(self, executor_id: str) -> bool:
        return executor_id in self.online

    async def inventory(self, executor_id: str):
        return self.hellos.get(executor_id)

    async def call(
        self,
        executor_id: str,
        op: str,
        args=None,
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
    ) -> ExecutorResult:
        payload = dict(args or {})
        self.calls.append((executor_id, op, payload, session_id))
        if self.call_impl is not None:
            return await self.call_impl(
                executor_id, op, payload, session_id, timeout_s
            )
        return _ok({"session_id": session_id, "workdir": "/workspace/project"})


@pytest.mark.asyncio
async def test_start_persists_creating_before_executor_call(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    _trust(state, executor_id)
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello()
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]

    async def observe(_executor_id, _op, _args, session_id, _timeout):
        assert session_id is not None
        assert state.snapshot_sessions()[session_id].status == "creating"
        return _ok({"session_id": session_id, "workdir": "/workspace/project"})

    transport.call_impl = observe
    result = await coordinator.start_session(workdir="project")

    assert isinstance(result, dict)
    session_id = str(result["session_id"])
    record = state.snapshot_sessions()[session_id]
    assert record.executor_id == executor_id
    assert record.status == "active"
    assert record.resolved_workdir_display == "/workspace/project"


@pytest.mark.asyncio
async def test_start_requires_exactly_one_eligible_executor(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    transport = FakeTransport()
    for _ in range(2):
        executor_id = new_executor_id()
        _trust(state, executor_id)
        transport.online.add(executor_id)
        transport.hellos[executor_id] = _hello()
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="exactly one eligible executor"):
        await coordinator.start_session(workdir="project")


@pytest.mark.asyncio
async def test_queued_create_timeout_removes_checkpoint(tmp_path: Path) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    _trust(state, executor_id)
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello()
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]

    async def queued_timeout(*_args):
        exc = TimeoutError("queued")
        exc._workgate_executor_delivery_state = "queued"  # type: ignore[attr-defined]
        raise exc

    transport.call_impl = queued_timeout
    with pytest.raises(TimeoutError):
        await coordinator.start_session(workdir="project")
    assert state.snapshot_sessions() == {}


@pytest.mark.asyncio
async def test_offered_create_timeout_uses_positive_lookup_without_replay(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    _trust(state, executor_id)
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello()
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]
    create_session_id: str | None = None

    async def offered_then_lookup(
        _executor_id, op, _args, session_id, _timeout
    ):
        nonlocal create_session_id
        if op == "session.create":
            create_session_id = session_id
            exc = TimeoutError("offered")
            exc._workgate_executor_delivery_state = "offered"  # type: ignore[attr-defined]
            raise exc
        assert op == "session.lookup"
        assert session_id == create_session_id
        return _ok(
            {"session_id": session_id, "resolved_workdir": "/workspace/project"}
        )

    transport.call_impl = offered_then_lookup
    with pytest.raises(TimeoutError):
        await coordinator.start_session(workdir="project")

    assert create_session_id is not None
    record = state.snapshot_sessions()[create_session_id]
    assert record.status == "active"
    assert [call[1] for call in transport.calls] == [
        "session.create",
        "session.lookup",
    ]


@pytest.mark.asyncio
async def test_hello_reconciles_creating_missing_and_terminating(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    _trust(state, executor_id)
    creating = new_session_id()
    active = new_session_id()
    terminating = new_session_id()
    for session_id, status in (
        (creating, "creating"),
        (active, "active"),
        (terminating, "terminating"),
    ):
        state.put_session(
            ControlSessionRecord(
                session_id=session_id,
                executor_id=executor_id,
                requested_workdir="project",
                resolved_workdir_display=(
                    "/workspace/old" if status == "active" else None
                ),
                label=None,
                status=status,  # type: ignore[arg-type]
                created_at=1,
                updated_at=1,
            )
        )
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello((creating, "/workspace/project"))
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]

    await coordinator.reconcile_hello(executor_id)

    sessions = state.snapshot_sessions()
    assert sessions[creating].status == "active"
    assert sessions[creating].resolved_workdir_display == "/workspace/project"
    assert sessions[active].status == "missing"
    assert sessions[terminating].status == "ended"


@pytest.mark.asyncio
async def test_end_persists_terminating_before_command(tmp_path: Path) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    session_id = new_session_id()
    _trust(state, executor_id)
    state.put_session(
        ControlSessionRecord(
            session_id=session_id,
            executor_id=executor_id,
            requested_workdir="project",
            resolved_workdir_display="/workspace/project",
            label=None,
            status="active",
            created_at=1,
            updated_at=1,
        )
    )
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello((session_id, "/workspace/project"))
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]

    async def observe(_executor_id, op, _args, observed_session_id, _timeout):
        assert op == "session.terminate"
        assert observed_session_id == session_id
        assert state.snapshot_sessions()[session_id].status == "terminating"
        return _ok({"session_id": session_id, "absent": True})

    transport.call_impl = observe
    result = await coordinator.end_session(session_id)

    assert result["ended"] is True
    assert state.snapshot_sessions()[session_id].status == "ended"


def _active_record(executor_id: str, session_id: str) -> ControlSessionRecord:
    return ControlSessionRecord(
        session_id=session_id,
        executor_id=executor_id,
        requested_workdir="project",
        resolved_workdir_display="/workspace/project",
        label=None,
        status="active",
        created_at=1,
        updated_at=1,
    )


@pytest.mark.parametrize(
    ("agent_session_retention_s", "now"),
    ((10, 100.0), (0, 20_000.0)),
)
@pytest.mark.asyncio
async def test_start_reaps_stale_creating_session_through_desired_absence(
    tmp_path: Path,
    agent_session_retention_s: int,
    now: float,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    existing = new_session_id()
    _trust(state, executor_id)
    state.put_session(
        ControlSessionRecord(
            session_id=existing,
            executor_id=executor_id,
            requested_workdir="project",
            resolved_workdir_display=None,
            label=None,
            status="creating",
            created_at=1,
            updated_at=1,
        )
    )
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello()

    async def execute(_executor_id, op, _args, session_id, _timeout):
        if op == "session.terminate":
            assert session_id == existing
            assert state.snapshot_sessions()[existing].status == "terminating"
            return _ok({"session_id": existing, "absent": True})
        assert op == "session.create"
        return _ok({"session_id": session_id, "workdir": "/workspace/new"})

    transport.call_impl = execute
    coordinator = ControlSessionCoordinator(
        state,
        transport,  # type: ignore[arg-type]
        max_agent_sessions=1,
        agent_session_retention_s=agent_session_retention_s,
        clock=lambda: now,
    )

    created = await coordinator.start_session(workdir="new")

    assert isinstance(created, dict)
    assert state.snapshot_sessions()[existing].status == "ended"
    assert (
        state.snapshot_sessions()[str(created["session_id"])].status == "active"
    )
    assert [call[1] for call in transport.calls] == [
        "session.terminate",
        "session.create",
    ]


@pytest.mark.asyncio
async def test_start_reaps_expired_session_through_confirmed_absence(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    existing = new_session_id()
    _trust(state, executor_id)
    state.put_session(_active_record(executor_id, existing))
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello((existing, "/workspace/project"))

    async def execute(_executor_id, op, _args, session_id, _timeout):
        if op == "session.lookup":
            assert session_id == existing
            return _ok(
                {
                    "session_id": existing,
                    "resolved_workdir": "/workspace/project",
                    "last_active_at": 1.0,
                    "has_persistent_shells": False,
                    "has_active_jobs": False,
                }
            )
        if op == "session.terminate":
            assert state.snapshot_sessions()[existing].status == "terminating"
            return _ok({"session_id": existing, "absent": True})
        assert op == "session.create"
        return _ok({"session_id": session_id, "workdir": "/workspace/new"})

    transport.call_impl = execute
    coordinator = ControlSessionCoordinator(
        state,
        transport,  # type: ignore[arg-type]
        max_agent_sessions=1,
        agent_session_retention_s=10,
        clock=lambda: 100.0,
    )

    created = await coordinator.start_session(workdir="new")

    assert isinstance(created, dict)
    assert state.snapshot_sessions()[existing].status == "ended"
    assert (
        state.snapshot_sessions()[str(created["session_id"])].status == "active"
    )
    assert [call[1] for call in transport.calls] == [
        "session.lookup",
        "session.terminate",
        "session.create",
    ]


@pytest.mark.asyncio
async def test_overflow_does_not_reap_session_with_owned_resources(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    existing = new_session_id()
    _trust(state, executor_id)
    state.put_session(_active_record(executor_id, existing))
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello((existing, "/workspace/project"))

    async def lookup(_executor_id, op, _args, session_id, _timeout):
        assert op == "session.lookup"
        assert session_id == existing
        return _ok(
            {
                "session_id": existing,
                "resolved_workdir": "/workspace/project",
                "last_active_at": 1.0,
                "has_persistent_shells": True,
                "has_active_jobs": False,
            }
        )

    transport.call_impl = lookup
    coordinator = ControlSessionCoordinator(
        state,
        transport,  # type: ignore[arg-type]
        max_agent_sessions=1,
        clock=lambda: 20_000.0,
    )

    with pytest.raises(RuntimeError, match="agent session limit reached"):
        await coordinator.start_session(workdir="new")

    assert state.snapshot_sessions()[existing].status == "active"
    assert [call[1] for call in transport.calls] == ["session.lookup"]


@pytest.mark.asyncio
async def test_overflow_revalidates_activity_before_termination(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    existing = new_session_id()
    _trust(state, executor_id)
    state.put_session(_active_record(executor_id, existing))
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello((existing, "/workspace/project"))
    lookups = 0

    async def lookup(_executor_id, op, _args, session_id, _timeout):
        nonlocal lookups
        assert op == "session.lookup"
        assert session_id == existing
        lookups += 1
        return _ok(
            {
                "session_id": existing,
                "resolved_workdir": "/workspace/project",
                "last_active_at": 1.0 if lookups == 1 else 19_999.0,
                "has_persistent_shells": False,
                "has_active_jobs": False,
            }
        )

    transport.call_impl = lookup
    coordinator = ControlSessionCoordinator(
        state,
        transport,  # type: ignore[arg-type]
        max_agent_sessions=1,
        clock=lambda: 20_000.0,
    )

    with pytest.raises(RuntimeError, match="agent session limit reached"):
        await coordinator.start_session(workdir="new")

    assert lookups == 2
    assert state.snapshot_sessions()[existing].status == "active"
    assert all(call[1] != "session.terminate" for call in transport.calls)


@pytest.mark.asyncio
async def test_overflow_respects_control_managed_resource_hook(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    existing = new_session_id()
    _trust(state, executor_id)
    state.put_session(_active_record(executor_id, existing))
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello((existing, "/workspace/project"))

    async def lookup(_executor_id, op, _args, session_id, _timeout):
        assert op == "session.lookup"
        assert session_id == existing
        return _ok(
            {
                "session_id": existing,
                "resolved_workdir": "/workspace/project",
                "last_active_at": 1.0,
                "has_persistent_shells": False,
                "has_active_jobs": False,
            }
        )

    async def blocked(session_id: str) -> bool:
        assert session_id == existing
        return True

    async def no_cleanup(_session_id: str) -> list[str]:
        return []

    transport.call_impl = lookup
    coordinator = ControlSessionCoordinator(
        state,
        transport,  # type: ignore[arg-type]
        max_agent_sessions=1,
        clock=lambda: 20_000.0,
    )
    coordinator.set_control_resource_hooks(
        auto_cleanup_blocked=blocked,
        before_terminate=no_cleanup,
    )

    with pytest.raises(RuntimeError, match="agent session limit reached"):
        await coordinator.start_session(workdir="new")

    assert state.snapshot_sessions()[existing].status == "active"
    assert [call[1] for call in transport.calls] == ["session.lookup"]


@pytest.mark.asyncio
async def test_end_stops_control_resources_after_terminating(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    session_id = new_session_id()
    _trust(state, executor_id)
    state.put_session(_active_record(executor_id, session_id))
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello((session_id, "/workspace/project"))
    cleanup_observations: list[str] = []

    async def not_blocked(_session_id: str) -> bool:
        return False

    async def stop_control_resources(observed_session_id: str) -> list[str]:
        assert observed_session_id == session_id
        cleanup_observations.append(
            state.snapshot_sessions()[session_id].status
        )
        return ["managed-copy"]

    async def terminate(_executor_id, op, _args, observed_session_id, _timeout):
        assert op == "session.terminate"
        assert observed_session_id == session_id
        assert state.snapshot_sessions()[session_id].status == "terminating"
        return _ok(
            {
                "session_id": session_id,
                "absent": True,
                "stopped_jobs": ["executor-job"],
                "stopped_shells": [],
            }
        )

    transport.call_impl = terminate
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]
    coordinator.set_control_resource_hooks(
        auto_cleanup_blocked=not_blocked,
        before_terminate=stop_control_resources,
    )

    ended = await coordinator.end_session(session_id)

    assert cleanup_observations == ["terminating"]
    assert ended["stopped_jobs"] == ["managed-copy", "executor-job"]
    assert state.snapshot_sessions()[session_id].status == "ended"
