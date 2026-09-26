import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import workgate.control.state as control_state_module
from workgate.config.settings import Settings
from workgate.control.executor_transport import (
    ExecutorTransportClosedError,
    ExecutorTransportError,
)
from workgate.control.state import ControlSessionRecord
from workgate.hosted import (
    DurableObjectSqlStateStore,
    build_hosted_control_actor_core,
)
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import (
    EXECUTOR_CAPABILITY_SESSIONS,
    ExecutorHelloRequest,
    ExecutorResult,
    ExecutorRuntimeSummary,
    SessionInventorySummary,
)
from workgate.protocol.ids import new_session_id
from workgate.protocol.pairing import (
    PairApprovalRequest,
    PairDecision,
    PairingExecutorMetadata,
    PairStartRequest,
)


class _SqliteCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def toArray(self) -> list[dict[str, Any]]:
        columns = tuple(row[0] for row in (self._cursor.description or ()))
        return [
            dict(zip(columns, row, strict=True))
            for row in self._cursor.fetchall()
        ]


class _SqliteSqlStorage:
    """Local synchronous stand-in for Durable Object ctx.storage.sql."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(path, autocommit=True)

    def exec(self, query: str, *bindings: object) -> _SqliteCursor:
        return _SqliteCursor(self.connection.execute(query, bindings))

    def close(self) -> None:
        self.connection.close()


class _StaticCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def toArray(self) -> list[Any]:
        return list(self._rows)


class _AttributeRowSqlStorage:
    """Provider-shaped cursor whose row fields are exposed as attributes."""

    def exec(self, query: str, *bindings: object) -> _StaticCursor:
        _ = bindings
        if "SELECT json" in query:
            return _StaticCursor(
                [SimpleNamespace(json='{"value": "cloudflare"}')]
            )
        return _StaticCursor([])


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        base_url="https://control.example",
        executor_max_pending_commands=4,
    )


def _hello(session_id: str | None = None) -> ExecutorHelloRequest:
    sessions = (
        ()
        if session_id is None
        else (
            SessionInventorySummary(
                session_id=session_id,
                resolved_workdir="/workspace/project",
            ),
        )
    )
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        capabilities=(EXECUTOR_CAPABILITY_SESSIONS,),
        workspace_root="/workspace",
        sessions=sessions,
        shells=(),
        jobs=(),
    )


def test_durable_object_sql_state_store_crud_and_logical_directories() -> None:
    sql = _SqliteSqlStorage()
    store = DurableObjectSqlStateStore(sql)
    session_a = store.layout.session_metadata_path("session_a")
    session_b = store.layout.session_todos_path("session_b")

    store.write_json(session_a, {"value": "alpha"})
    store.write_json(session_b, {"value": "beta"})
    store.write_json(Path("control/executors.json"), {"version": 1})

    assert store.read_json(session_a) == {"value": "alpha"}
    assert store.read_json(store.layout.control_executors_path) == {
        "version": 1
    }
    assert tuple(store.iter_directories(store.layout.root)) == (
        store.layout.control_dir,
        store.layout.sessions_dir,
    )
    assert tuple(store.iter_directories(store.layout.sessions_dir)) == (
        store.layout.session_dir("session_a"),
        store.layout.session_dir("session_b"),
    )
    with pytest.raises(ValueError, match="Refusing to read"):
        store.read_json(session_a, max_bytes=1)

    with pytest.raises(OSError, match="not empty"):
        store.remove(store.layout.session_dir("session_a"))
    store.remove(store.layout.session_dir("session_a"), recursive=True)
    assert store.read_json(session_a) is None
    assert store.read_json(session_b) == {"value": "beta"}

    store.remove(session_b)
    assert store.read_json(session_b) is None
    with pytest.raises(OSError, match="not empty"):
        store.remove(store.layout.root)
    store.remove(store.layout.root, recursive=True)
    assert store.read_json(store.layout.control_executors_path) is None


def test_durable_object_sql_state_store_rejects_escape() -> None:
    store = DurableObjectSqlStateStore(_SqliteSqlStorage())

    with pytest.raises(ValueError, match="invalid state path"):
        store.read_json(Path("../outside/value.json"))


def test_durable_object_sql_state_store_accepts_attribute_rows() -> None:
    store = DurableObjectSqlStateStore(_AttributeRowSqlStorage())

    assert store.read_json(store.layout.control_executors_path) == {
        "value": "cloudflare"
    }


@pytest.mark.asyncio
async def test_hosted_actor_does_not_construct_threading_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_rlock() -> None:
        raise RuntimeError("threading is unavailable")

    monkeypatch.setattr(control_state_module, "RLock", unavailable_rlock)
    actor = build_hosted_control_actor_core(
        _settings(tmp_path),
        state_store=DurableObjectSqlStateStore(_SqliteSqlStorage()),
    )

    actor.start()
    await actor.aclose()


@pytest.mark.asyncio
async def test_hosted_actor_lifecycle_is_idempotent_and_not_restartable(
    tmp_path: Path,
) -> None:
    actor = build_hosted_control_actor_core(
        _settings(tmp_path),
        state_store=DurableObjectSqlStateStore(_SqliteSqlStorage()),
    )

    actor.start()
    actor.start()
    await actor.aclose()
    await actor.aclose()

    with pytest.raises(RuntimeError, match="cannot restart"):
        actor.start()


@pytest.mark.asyncio
async def test_hosted_actor_reconstruction_keeps_facts_but_drops_live_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hosted.sqlite3"
    sql = _SqliteSqlStorage(database)
    first_store = DurableObjectSqlStateStore(sql)
    settings = _settings(tmp_path)
    first = build_hosted_control_actor_core(settings, state_store=first_store)
    first.start()

    pair = await first.executor_pairing.start_pairing(
        PairStartRequest(
            requested_name="laptop",
            metadata=PairingExecutorMetadata(
                hostname="laptop", platform="linux"
            ),
        )
    )
    await first.executor_pairing.decide(
        PairApprovalRequest(
            user_code=pair.user_code,
            decision=PairDecision.APPROVE,
        )
    )
    delivered = await first.executor_pairing.poll(pair.device_code)
    await first.executor_transport.hello(delivered.credential, _hello())

    session = ControlSessionRecord(
        session_id=new_session_id(),
        executor_id=delivered.executor_id,
        requested_workdir="project",
        resolved_workdir_display="/workspace/project",
        status="active",
        created_at=10,
        updated_at=10,
    )
    first.control_state.put_session(session)

    offered_pending = asyncio.create_task(
        first.executor_transport.call(
            delivered.executor_id,
            "shell.run",
            {"command": "echo offered"},
        )
    )
    queued_pending = asyncio.create_task(
        first.executor_transport.call(
            delivered.executor_id,
            "shell.run",
            {"command": "echo queued"},
        )
    )
    await asyncio.sleep(0)
    assert (
        await first.executor_transport.pending_count(delivered.executor_id) == 2
    )
    offered_command = await first.executor_transport.poll(delivered.credential)
    assert offered_command is not None
    assert offered_command.args == {"command": "echo offered"}

    unfinished_pair = await first.executor_pairing.start_pairing(
        PairStartRequest(requested_name="second-laptop")
    )
    assert unfinished_pair.device_code
    stream = await first.stream_hub.create(delivered.executor_id)
    assert await first.stream_hub.browser_expires_at(stream.stream_id)

    stored_rows = sql.exec("SELECT json FROM workgate_state").toArray()
    assert delivered.credential not in repr(stored_rows)

    await first.aclose()
    with pytest.raises(ExecutorTransportClosedError):
        await offered_pending
    with pytest.raises(ExecutorTransportClosedError):
        await queued_pending
    sql.close()

    reconstructed_sql = _SqliteSqlStorage(database)
    second_store = DurableObjectSqlStateStore(reconstructed_sql)
    second = build_hosted_control_actor_core(settings, state_store=second_store)
    second.start()
    try:
        restored_trust = second.control_state.snapshot_executors()
        restored_sessions = second.control_state.snapshot_sessions()
        assert delivered.executor_id in restored_trust
        assert restored_sessions == {session.session_id: session}

        assert not await second.executor_transport.is_online(
            delivered.executor_id
        )
        assert (
            await second.executor_transport.pending_count(delivered.executor_id)
            == 0
        )
        assert await second.executor_pairing.attempt_count() == 0
        assert (
            await second.stream_hub.browser_expires_at(stream.stream_id) is None
        )

        retried_pair = await second.executor_pairing.start_pairing(
            PairStartRequest(requested_name="second-laptop")
        )
        assert retried_pair.device_code
        assert await second.executor_pairing.attempt_count() == 1

        await second.executor_transport.validate(delivered.credential)
        await second.executor_transport.hello(
            delivered.credential,
            _hello(session.session_id),
        )
        assert await second.executor_transport.is_online(delivered.executor_id)
        assert second.control_state.snapshot_sessions() == {
            session.session_id: session
        }

        with pytest.raises(ExecutorTransportError) as late_result:
            await second.executor_transport.submit_result(
                delivered.credential,
                ExecutorResult(
                    id=offered_command.id,
                    ok=True,
                    result={"late": True},
                ),
            )
        assert late_result.value.error.code is ProtocolErrorCode.UNKNOWN_COMMAND
    finally:
        await second.aclose()
        reconstructed_sql.close()
