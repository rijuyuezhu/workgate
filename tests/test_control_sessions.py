from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from workgate.control.executor_transport import (
    ExecutorTransport,
    ExecutorTransportError,
)
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
from workgate.protocol.errors import ProtocolErrorCode
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
@pytest.mark.parametrize("interrupt", ["revoke", "replace"])
async def test_offered_create_interrupted_by_trust_change_keeps_checkpoint(
    tmp_path: Path,
    interrupt: str,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    credential = new_executor_credential()
    trust = ExecutorTrustRecord(
        executor_id=executor_id,
        name="executor",
        credential_verifier=executor_credential_verifier(credential),
        created_at=1,
    )
    state.put_executor(trust)
    transport = ExecutorTransport(state, max_pending_commands=4)
    transport.start()
    await transport.hello(credential, _hello())
    coordinator = ControlSessionCoordinator(state, transport)

    start = asyncio.create_task(coordinator.start_session(workdir="project"))
    command = await transport.poll(credential)
    assert command is not None
    assert command.op == "session.create"
    session_id = str(command.session_id)
    assert state.snapshot_sessions()[session_id].status == "creating"

    if interrupt == "revoke":
        await transport.revoke_executor(executor_id, revoked_at=2)
    else:
        replacement = new_executor_credential()
        await transport.replace_executor(
            trust.model_copy(
                update={
                    "credential_verifier": executor_credential_verifier(
                        replacement
                    )
                }
            )
        )

    with pytest.raises(ExecutorTransportError):
        await start
    assert state.snapshot_sessions()[session_id].status == "creating"
    await coordinator.aclose()
    await transport.aclose()


@pytest.mark.asyncio
async def test_hello_reconciles_creating_and_terminating_with_derived_missing(
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
    assert sessions[active].status == "active"
    assert (
        await coordinator.session_availability(active) == "missing_on_executor"
    )
    assert sessions[terminating].status == "ended"

    state.close()
    restarted = _state(tmp_path)
    assert restarted.snapshot_sessions()[active].status == "active"


@pytest.mark.asyncio
async def test_hello_merges_activity_monotonically(tmp_path: Path) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    session_id = new_session_id()
    _trust(state, executor_id)
    state.put_session(_active_record(executor_id, session_id))
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        capabilities=(EXECUTOR_CAPABILITY_SESSIONS,),
        workspace_root="/workspace",
        sessions=(
            SessionInventorySummary(
                session_id=session_id,
                resolved_workdir="/workspace/project",
                last_active_at=123.0,
            ),
        ),
        shells=(),
        jobs=(),
    )
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]

    await coordinator.reconcile_hello(executor_id)
    (
        availability,
        last_active_at,
    ) = await coordinator.session_activity_projection(session_id)
    assert availability == "available"
    assert last_active_at == 123.0

    coordinator.observe_session_activity(session_id, observed_at=20_000.0)
    await coordinator.reconcile_hello(executor_id)
    (
        availability,
        last_active_at,
    ) = await coordinator.session_activity_projection(session_id)
    assert availability == "available"
    assert last_active_at == 20_000.0

    transport.hellos[executor_id] = transport.hellos[executor_id].model_copy(
        update={
            "sessions": (
                SessionInventorySummary(
                    session_id=session_id,
                    resolved_workdir="/workspace/project",
                    last_active_at=30_000.0,
                ),
            )
        }
    )
    await coordinator.reconcile_hello(executor_id)
    (
        availability,
        last_active_at,
    ) = await coordinator.session_activity_projection(session_id)
    assert availability == "available"
    assert last_active_at == 30_000.0
    assert transport.calls == []


@pytest.mark.asyncio
async def test_newer_hello_repairs_activity_after_offered_command_abandon(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    session_id = new_session_id()
    credential = new_executor_credential()
    state.put_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="executor",
            credential_verifier=executor_credential_verifier(credential),
            created_at=1,
        )
    )
    state.put_session(_active_record(executor_id, session_id))
    transport = ExecutorTransport(state, max_pending_commands=4)
    transport.start()
    coordinator = ControlSessionCoordinator(state, transport)
    initial_hello = ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        capabilities=(EXECUTOR_CAPABILITY_SESSIONS,),
        workspace_root="/workspace",
        sessions=(
            SessionInventorySummary(
                session_id=session_id,
                resolved_workdir="/workspace/project",
                last_active_at=1_000.0,
            ),
        ),
        shells=(),
        jobs=(),
    )
    try:
        await transport.hello(credential, initial_hello)
        coordinator.observe_session_activity(session_id, observed_at=1_000.0)

        caller = asyncio.create_task(
            transport.call(
                executor_id,
                "files.read",
                {"path": "ignored"},
                session_id=session_id,
            )
        )
        await asyncio.sleep(0)
        command = await transport.poll(credential)
        assert command is not None
        assert command.session_id == session_id

        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        with pytest.raises(ExecutorTransportError) as caught:
            await transport.submit_result(
                credential,
                ExecutorResult(id=command.id, ok=True, result={}),
            )
        assert caught.value.error.code is ProtocolErrorCode.UNKNOWN_COMMAND

        await transport.hello(
            credential,
            initial_hello.model_copy(
                update={
                    "sessions": (
                        SessionInventorySummary(
                            session_id=session_id,
                            resolved_workdir="/workspace/project",
                            last_active_at=20_000.0,
                        ),
                    )
                }
            ),
        )
        await coordinator.reconcile_hello(executor_id)
        (
            availability,
            last_active_at,
        ) = await coordinator.session_activity_projection(session_id)
        assert availability == "available"
        assert last_active_at == 20_000.0
    finally:
        await coordinator.aclose()
        await transport.aclose()


@pytest.mark.asyncio
async def test_transport_uncertainty_does_not_refresh_activity(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    session_id = new_session_id()
    _trust(state, executor_id)
    state.put_session(_active_record(executor_id, session_id))
    transport = FakeTransport()
    transport.online.add(executor_id)

    async def fail_transport(_executor_id, _op, _args, _session_id, _timeout):
        raise RuntimeError("transport down")

    transport.call_impl = fail_transport
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]
    coordinator.observe_session_activity(session_id, observed_at=10.0)

    with pytest.raises(RuntimeError, match="transport down"):
        await coordinator.call_session_tool(
            "read", {"session_id": session_id, "path": "missing.txt"}
        )

    (
        availability,
        last_active_at,
    ) = await coordinator.session_activity_projection(session_id)
    assert availability == "available"
    assert last_active_at == 10.0


@pytest.mark.asyncio
async def test_hello_reports_orphan_and_cross_executor_session_diagnostics(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    other_executor_id = new_executor_id()
    _trust(state, executor_id)
    _trust(state, other_executor_id)
    orphan = new_session_id()
    misbound = new_session_id()
    state.put_session(_active_record(other_executor_id, misbound))
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello(
        (orphan, "/workspace/orphan"),
        (misbound, "/workspace/misbound"),
    )
    coordinator = ControlSessionCoordinator(state, transport)  # type: ignore[arg-type]

    with caplog.at_level("WARNING", logger="workgate.control.sessions"):
        await coordinator.reconcile_hello(executor_id)

    assert f"reported orphan session {orphan} unknown to control" in caplog.text
    assert (
        f"reported session {misbound} bound to executor {other_executor_id}"
        in caplog.text
    )
    assert orphan not in state.snapshot_sessions()
    assert state.snapshot_sessions()[misbound].executor_id == other_executor_id


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
async def test_explicit_cleanup_lookup_confirms_missing_availability(
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
        assert op == "session.lookup"
        assert session_id == existing
        return _ok(None)

    transport.call_impl = execute
    coordinator = ControlSessionCoordinator(
        state,
        transport,  # type: ignore[arg-type]
        max_agent_sessions=1,
        agent_session_retention_s=10,
        clock=lambda: 100.0,
    )

    with pytest.raises(RuntimeError, match="agent session limit reached"):
        await coordinator.start_session(workdir="new")

    assert [call[1] for call in transport.calls] == ["session.lookup"]
    assert (
        await coordinator.session_availability(existing)
        == "missing_on_executor"
    )
    assert state.snapshot_sessions()[existing].status == "active"


@pytest.mark.asyncio
async def test_missing_observation_reaps_after_retention_without_durable_state(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    executor_id = new_executor_id()
    existing = new_session_id()
    _trust(state, executor_id)
    state.put_session(_active_record(executor_id, existing))
    transport = FakeTransport()
    transport.online.add(executor_id)
    transport.hellos[executor_id] = _hello()
    clock = {"now": 100.0}

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
        agent_session_retention_s=10,
        clock=lambda: clock["now"],
    )

    await coordinator.reconcile_hello(executor_id)
    assert (
        await coordinator.session_availability(existing)
        == "missing_on_executor"
    )
    assert state.snapshot_sessions()[existing].status == "active"

    clock["now"] = 105.0
    await coordinator.reconcile_hello(executor_id)
    clock["now"] = 109.0
    with pytest.raises(RuntimeError, match="agent session limit reached"):
        await coordinator.start_session(workdir="new")
    assert transport.calls == []
    assert state.snapshot_sessions()[existing].status == "active"

    clock["now"] = 111.0
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
