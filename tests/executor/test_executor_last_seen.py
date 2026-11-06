"""Durable last contact metadata must not become durable online authority."""

import math

import pytest

from tests.executor.test_executor_transport import (
    _Clock,
    _hello,
    _running_transport,
)
from workgate.control.executor_transport import (
    ExecutorTransport,
    ExecutorTransportError,
)
from workgate.control.state import ControlState, ExecutorTrustRecord
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.ids import new_executor_id


@pytest.mark.asyncio
async def test_last_seen_is_throttled_flushed_and_not_treated_as_online(
    tmp_path, monkeypatch
):
    monotonic = _Clock(value=100)
    wall = _Clock(value=1_000)
    transport, state, executor_id, credential = _running_transport(
        tmp_path, clock=monotonic
    )
    transport._wall_clock = wall
    writes = []
    original = state._write_executors

    def observed(records):
        writes.append(records[executor_id].last_seen_at)
        original(records)

    monkeypatch.setattr(state, "_write_executors", observed)
    assert await transport.last_seen_at(executor_id) is None
    await transport.validate(credential)
    assert not writes  # Authentication-only checks never imply online presence.
    await transport.hello(credential, _hello())
    assert writes == [1_000]

    for seconds in (10, 20, 40, 59):
        monotonic.value = 100 + seconds
        wall.value = 1_000 + seconds
        await transport.heartbeat(credential)
    assert writes == [1_000]  # No one-file-write-per-heartbeat.
    assert await transport.last_seen_at(executor_id) == 1_059

    monotonic.value = 161
    wall.value = 1_061
    await transport.heartbeat(credential)
    assert writes == [1_000, 1_061]
    monotonic.value = 175
    wall.value = 1_075
    await transport.heartbeat(credential)
    assert writes == [1_000, 1_061]
    await transport.aclose()
    assert writes == [1_000, 1_061, 1_075]  # Graceful shutdown flush.
    state.close()

    restored_state = ControlState(FileStateStore(lambda: tmp_path / "state"))
    restored_state.start()
    restored = ExecutorTransport(
        restored_state,
        max_pending_commands=2,
        clock=_Clock(value=175),
    )
    restored.start()
    assert (
        restored_state.snapshot_executors()[executor_id].last_seen_at == 1_075
    )
    assert await restored.last_seen_at(executor_id) == 1_075
    assert not await restored.is_online(executor_id)
    assert await restored.inventory(executor_id) is None
    await restored.aclose()
    restored_state.close()


@pytest.mark.asyncio
async def test_recent_contact_does_not_go_backwards_on_clock_rollback(tmp_path):
    clock = _Clock(value=100)
    wall = _Clock(value=2_000)
    transport, state, executor_id, credential = _running_transport(
        tmp_path, clock=clock
    )
    transport._wall_clock = wall
    await transport.hello(credential, _hello())
    assert state.snapshot_executors()[executor_id].last_seen_at == 2_000
    clock.value = 200
    wall.value = 900
    await transport.heartbeat(credential)
    assert await transport.is_online(executor_id)
    assert await transport.last_seen_at(executor_id) == 2_000
    assert state.snapshot_executors()[executor_id].last_seen_at == 2_000
    for bad in (math.nan, math.inf, -1):
        wall.value = bad
        await transport.heartbeat(credential)
        assert state.snapshot_executors()[executor_id].last_seen_at == 2_000
    await transport.aclose()


@pytest.mark.asyncio
async def test_revoked_and_invalid_bearers_never_update_last_seen(tmp_path):
    transport, state, executor_id, credential = _running_transport(tmp_path)
    transport._wall_clock = _Clock(value=1_000)
    await transport.hello(credential, _hello())
    existing = state.snapshot_executors()[executor_id].last_seen_at
    with pytest.raises(ExecutorTransportError) as exc:
        await transport.heartbeat("invalid-credential")
    assert exc.value.error.code == ProtocolErrorCode.UNAUTHORIZED_EXECUTOR
    assert state.snapshot_executors()[executor_id].last_seen_at == existing

    await transport.revoke_executor(executor_id, revoked_at=1_500)
    with pytest.raises(ExecutorTransportError) as exc:
        await transport.heartbeat(credential)
    assert exc.value.error.code == ProtocolErrorCode.EXECUTOR_REVOKED
    with pytest.raises(ValueError, match="revoked"):
        state.record_executor_last_seen(executor_id, seen_at=1_600)
    await transport.aclose()
    assert state.snapshot_executors()[executor_id].last_seen_at == existing


def test_legacy_missing_last_seen_is_null_and_invalid_values_rejected(tmp_path):
    store = FileStateStore(lambda: tmp_path / "state")
    state = ControlState(store)
    state.start()
    executor_id = new_executor_id()
    state.put_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="legacy",
            credential_verifier=executor_credential_verifier(
                new_executor_credential()
            ),
            created_at=100,
        )
    )
    raw = store.read_json(store.layout.control_executors_path)
    assert isinstance(raw, dict)
    assert "last_seen_at" not in raw["executors"][0]
    for bad in (math.nan, -1.0, math.inf):
        with pytest.raises(ValueError, match="finite and nonnegative"):
            state.record_executor_last_seen(executor_id, seen_at=bad)
    assert state.record_executor_last_seen(executor_id, seen_at=1_000) == 1_000
    assert state.record_executor_last_seen(executor_id, seen_at=900) == 1_000
    state.close()
    restored = ControlState(store)
    restored.start()
    assert restored.snapshot_executors()[executor_id].last_seen_at == 1_000


@pytest.mark.asyncio
async def test_failed_durable_write_keeps_live_presence_and_retries(
    tmp_path, monkeypatch
):
    mono = _Clock(value=100)
    wall = _Clock(value=1_000)
    transport, state, executor_id, credential = _running_transport(
        tmp_path, clock=mono
    )
    transport._wall_clock = wall
    original = state.record_executor_last_seen
    attempts = []

    def flaky(executor_id, *, seen_at):
        attempts.append(seen_at)
        if len(attempts) == 1:
            raise OSError("simulated disk unavailable")
        return original(executor_id, seen_at=seen_at)

    monkeypatch.setattr(state, "record_executor_last_seen", flaky)
    await transport.hello(credential, _hello())
    assert await transport.is_online(executor_id)
    assert state.snapshot_executors()[executor_id].last_seen_at is None
    wall.value = 1_010
    await transport.heartbeat(credential)
    assert state.snapshot_executors()[executor_id].last_seen_at == 1_010
    assert attempts == [1_000, 1_010]
    await transport.aclose()


@pytest.mark.asyncio
async def test_last_seen_pending_flush_survives_runtime_trust_replacement(
    tmp_path,
):
    mono = _Clock(value=100)
    wall = _Clock(value=1_000)
    transport, state, executor_id, credential = _running_transport(
        tmp_path, clock=mono
    )
    transport._wall_clock = wall
    await transport.hello(credential, _hello())
    wall.value = 1_005
    await transport.heartbeat(credential)
    prior_record = state.snapshot_executors()[executor_id]
    replacement = prior_record.model_copy(
        update={
            "credential_verifier": executor_credential_verifier(
                new_executor_credential()
            )
        }
    )
    await transport.replace_executor(replacement)
    await transport.aclose()
    assert state.snapshot_executors()[executor_id].last_seen_at == 1_000
