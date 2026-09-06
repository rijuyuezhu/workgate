import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from workgate.control.executor_transport import (
    ExecutorTransport,
    ExecutorTransportClosedError,
    ExecutorTransportError,
)
from workgate.control.state import ControlState, ExecutorTrustRecord
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import (
    ExecutorHelloRequest,
    ExecutorResult,
    ExecutorRuntimeSummary,
)
from workgate.protocol.ids import new_executor_id


@dataclass
class _Clock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value


def _hello() -> ExecutorHelloRequest:
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        capabilities=("shell",),
        workspace_root="/workspace",
        sessions=(),
        shells=(),
        jobs=(),
    )


def _running_transport(
    tmp_path: Path,
    *,
    max_pending_commands: int = 2,
    clock: _Clock | None = None,
) -> tuple[ExecutorTransport, ControlState, str, str]:
    store = FileStateStore(lambda: tmp_path / "state")
    state = ControlState(store)
    state.start()
    executor_id = new_executor_id()
    credential = new_executor_credential()
    state.put_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="executor",
            credential_verifier=executor_credential_verifier(credential),
            created_at=1,
        )
    )
    transport = ExecutorTransport(
        state,
        max_pending_commands=max_pending_commands,
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        clock=clock or _Clock(),
    )
    transport.start()
    return transport, state, executor_id, credential


async def _mark_online(transport: ExecutorTransport, credential: str) -> None:
    response = await transport.hello(credential, _hello())
    assert response.heartbeat_interval_s == 10
    assert response.offline_after_s == 30
    assert response.poll_timeout_s == 1


@pytest.mark.asyncio
async def test_hello_heartbeat_and_presence_are_process_local(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    transport, _, executor_id, credential = _running_transport(
        tmp_path, clock=clock
    )

    assert not await transport.is_online(executor_id)
    await _mark_online(transport, credential)
    assert await transport.is_online(executor_id)
    assert await transport.inventory(executor_id) == _hello()

    clock.value += 29
    assert await transport.is_online(executor_id)
    await transport.heartbeat(credential)
    clock.value += 29
    assert await transport.is_online(executor_id)
    clock.value += 2
    assert not await transport.is_online(executor_id)


@pytest.mark.asyncio
async def test_offline_call_fails_without_allocating_pending_state(
    tmp_path: Path,
) -> None:
    transport, _, executor_id, _ = _running_transport(tmp_path)

    with pytest.raises(ExecutorTransportError) as caught:
        await transport.call(executor_id, "shell.run", {"command": "true"})

    assert caught.value.error.code is ProtocolErrorCode.EXECUTOR_OFFLINE
    assert await transport.pending_count(executor_id) == 0


@pytest.mark.asyncio
async def test_admission_counts_queued_and_offered_correlations(
    tmp_path: Path,
) -> None:
    transport, _, executor_id, credential = _running_transport(
        tmp_path, max_pending_commands=1
    )
    await _mark_online(transport, credential)

    first = asyncio.create_task(transport.call(executor_id, "shell.run"))
    await asyncio.sleep(0)
    assert await transport.pending_count(executor_id) == 1

    with pytest.raises(ExecutorTransportError) as caught:
        await transport.call(executor_id, "shell.run")
    assert caught.value.error.code is ProtocolErrorCode.EXECUTOR_OVERLOADED
    assert await transport.pending_count(executor_id) == 1

    command = await transport.poll(credential)
    assert command is not None
    assert await transport.pending_count(executor_id) == 1

    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first
    assert await transport.pending_count(executor_id) == 0


@pytest.mark.asyncio
async def test_queued_cancellation_removes_command_before_offer(
    tmp_path: Path,
) -> None:
    transport, _, executor_id, credential = _running_transport(tmp_path)
    await _mark_online(transport, credential)

    caller = asyncio.create_task(transport.call(executor_id, "shell.run"))
    await asyncio.sleep(0)
    caller.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await caller

    assert await transport.pending_count(executor_id) == 0
    assert await transport.poll(credential) is None


@pytest.mark.asyncio
async def test_offered_command_is_never_requeued_after_caller_abandons(
    tmp_path: Path,
) -> None:
    transport, _, executor_id, credential = _running_transport(tmp_path)
    await _mark_online(transport, credential)

    caller = asyncio.create_task(transport.call(executor_id, "shell.run"))
    await asyncio.sleep(0)
    command = await transport.poll(credential)
    assert command is not None

    caller.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await caller

    assert await transport.pending_count(executor_id) == 0
    with pytest.raises(ExecutorTransportError) as caught:
        await transport.submit_result(
            credential, ExecutorResult(id=command.id, ok=True, result="late")
        )
    assert caught.value.error.code is ProtocolErrorCode.UNKNOWN_COMMAND


@pytest.mark.asyncio
async def test_only_one_delivery_poll_may_wait_per_executor(
    tmp_path: Path,
) -> None:
    transport, _, _, credential = _running_transport(tmp_path)
    await _mark_online(transport, credential)

    first_poll = asyncio.create_task(transport.poll(credential))
    await asyncio.sleep(0)

    with pytest.raises(ExecutorTransportError) as caught:
        await transport.poll(credential)
    assert caught.value.error.code is ProtocolErrorCode.EXECUTOR_OVERLOADED

    first_poll.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first_poll


@pytest.mark.asyncio
async def test_result_requires_current_expected_executor_and_live_command(
    tmp_path: Path,
) -> None:
    transport, state, executor_id, credential = _running_transport(tmp_path)
    other_id = new_executor_id()
    other_credential = new_executor_credential()
    state.put_executor(
        ExecutorTrustRecord(
            executor_id=other_id,
            name="other",
            credential_verifier=executor_credential_verifier(other_credential),
            created_at=1,
        )
    )
    await _mark_online(transport, credential)

    caller = asyncio.create_task(transport.call(executor_id, "shell.run"))
    await asyncio.sleep(0)
    command = await transport.poll(credential)
    assert command is not None

    with pytest.raises(ExecutorTransportError) as wrong_executor:
        await transport.submit_result(
            other_credential, ExecutorResult(id=command.id, ok=True, result=1)
        )
    assert wrong_executor.value.error.code is ProtocolErrorCode.UNKNOWN_COMMAND

    with pytest.raises(ExecutorTransportError) as invalid_bearer:
        await transport.submit_result(
            "not-a-credential", ExecutorResult(id=command.id, ok=True, result=1)
        )
    assert (
        invalid_bearer.value.error.code
        is ProtocolErrorCode.UNAUTHORIZED_EXECUTOR
    )

    expected = ExecutorResult(id=command.id, ok=True, result={"done": True})
    await transport.submit_result(credential, expected)
    assert await caller == expected
    assert await transport.pending_count(executor_id) == 0

    with pytest.raises(ExecutorTransportError) as duplicate:
        await transport.submit_result(credential, expected)
    assert duplicate.value.error.code is ProtocolErrorCode.UNKNOWN_COMMAND


@pytest.mark.asyncio
async def test_revoke_fences_handoff_and_interrupts_pending_call(
    tmp_path: Path,
) -> None:
    transport, _, executor_id, credential = _running_transport(tmp_path)
    await _mark_online(transport, credential)

    caller = asyncio.create_task(transport.call(executor_id, "shell.run"))
    await asyncio.sleep(0)
    await transport.revoke_executor(executor_id, revoked_at=5)

    with pytest.raises(ExecutorTransportError) as interrupted:
        await caller
    assert interrupted.value.error.code is ProtocolErrorCode.EXECUTOR_REVOKED
    assert await transport.pending_count(executor_id) == 0
    assert not await transport.is_online(executor_id)
    assert await transport.inventory(executor_id) is None

    with pytest.raises(ExecutorTransportError) as heartbeat:
        await transport.heartbeat(credential)
    assert heartbeat.value.error.code is ProtocolErrorCode.EXECUTOR_REVOKED


@pytest.mark.asyncio
async def test_replacement_invalidates_old_bearer_and_live_presence(
    tmp_path: Path,
) -> None:
    transport, _, executor_id, old_credential = _running_transport(tmp_path)
    await _mark_online(transport, old_credential)
    new_credential = new_executor_credential()

    await transport.replace_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="executor",
            credential_verifier=executor_credential_verifier(new_credential),
            created_at=2,
        )
    )

    assert not await transport.is_online(executor_id)
    with pytest.raises(ExecutorTransportError) as old:
        await transport.heartbeat(old_credential)
    assert old.value.error.code is ProtocolErrorCode.UNAUTHORIZED_EXECUTOR

    await _mark_online(transport, new_credential)
    assert await transport.is_online(executor_id)


@pytest.mark.asyncio
async def test_shutdown_interrupts_pending_future_without_durable_replay(
    tmp_path: Path,
) -> None:
    transport, _, executor_id, credential = _running_transport(tmp_path)
    await _mark_online(transport, credential)
    caller = asyncio.create_task(transport.call(executor_id, "shell.run"))
    await asyncio.sleep(0)

    await transport.aclose()

    with pytest.raises(ExecutorTransportClosedError):
        await caller
    assert await transport.pending_count(executor_id) == 0
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        transport.start()
