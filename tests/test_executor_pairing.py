from __future__ import annotations

from pathlib import Path

import pytest

from workgate.control.executor_transport import ExecutorTransport
from workgate.control.pairing import (
    ExecutorPairingError,
    ExecutorPairingService,
)
from workgate.control.state import ControlState, ExecutorTrustRecord
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import (
    executor_credential_matches,
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.ids import new_executor_id
from workgate.protocol.pairing import (
    PairApprovalRequest,
    PairDecision,
    PairingExecutorMetadata,
    PairStartRequest,
)


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _state(tmp_path: Path) -> ControlState:
    state = ControlState(FileStateStore(lambda: tmp_path / "state"))
    state.start()
    return state


def _service(
    tmp_path: Path,
    *,
    clock: _Clock | None = None,
    max_pending_attempts: int = 8,
    ttl_s: int = 60,
    start_rate_limit: int = 30,
    user_code_rate_limit: int = 60,
) -> tuple[ExecutorPairingService, ControlState, ExecutorTransport, _Clock]:
    active_clock = clock or _Clock()
    state = _state(tmp_path)
    transport = ExecutorTransport(
        state,
        max_pending_commands=4,
        clock=active_clock,
    )
    transport.start()
    service = ExecutorPairingService(
        state,
        transport,
        verification_uri="https://control.test/pair",
        max_pending_attempts=max_pending_attempts,
        ttl_s=ttl_s,
        poll_interval_s=2,
        start_rate_limit=start_rate_limit,
        user_code_rate_limit=user_code_rate_limit,
        clock=active_clock,
        wall_clock=lambda: 1_700_000_000.0,
    )
    return service, state, transport, active_clock


def _request(*, existing_executor_id: str | None = None) -> PairStartRequest:
    return PairStartRequest(
        requested_name="laptop",
        existing_executor_id=existing_executor_id,
        metadata=PairingExecutorMetadata(
            hostname="host-a", platform="linux", build="test"
        ),
    )


@pytest.mark.asyncio
async def test_pair_start_prunes_expired_attempts_before_capacity_admission(
    tmp_path: Path,
) -> None:
    service, _state_owner, transport, clock = _service(
        tmp_path, max_pending_attempts=1, ttl_s=5
    )
    first = await service.start_pairing(_request())

    with pytest.raises(ExecutorPairingError) as caught:
        await service.start_pairing(_request())
    assert (
        caught.value.error.code is ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED
    )

    clock.advance(5)
    second = await service.start_pairing(_request())
    assert second.device_code != first.device_code
    assert await service.attempt_count() == 1
    await service.aclose()
    await transport.aclose()


@pytest.mark.asyncio
async def test_pair_start_and_user_code_lookup_have_separate_rate_limits(
    tmp_path: Path,
) -> None:
    service, _state_owner, transport, clock = _service(
        tmp_path,
        ttl_s=120,
        start_rate_limit=1,
        user_code_rate_limit=1,
    )
    started = await service.start_pairing(_request())
    view = await service.lookup_user_code(started.user_code)
    assert view.requested_name == "laptop"
    assert view.metadata.hostname == "host-a"

    with pytest.raises(ExecutorPairingError) as start_error:
        await service.start_pairing(_request())
    assert (
        start_error.value.error.code
        is ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED
    )

    with pytest.raises(ExecutorPairingError) as lookup_error:
        await service.lookup_user_code(started.user_code)
    assert (
        lookup_error.value.error.code
        is ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED
    )

    clock.advance(61)
    await service.lookup_user_code(started.user_code)
    await service.aclose()
    await transport.aclose()


@pytest.mark.asyncio
async def test_pair_approval_persists_only_verifier_and_retries_same_delivery(
    tmp_path: Path,
) -> None:
    service, state, transport, clock = _service(tmp_path)
    started = await service.start_pairing(_request())

    approved = await service.decide(
        PairApprovalRequest(
            user_code=started.user_code,
            decision=PairDecision.APPROVE,
            name="approved-laptop",
        )
    )
    assert approved.status == "approved"
    assert approved.executor_id is not None

    first_delivery = await service.poll(started.device_code)
    clock.advance(2)
    second_delivery = await service.poll(started.device_code)
    assert second_delivery == first_delivery

    record = state.snapshot_executors()[first_delivery.executor_id]
    assert record.name == "approved-laptop"
    assert executor_credential_matches(
        first_delivery.credential, record.credential_verifier
    )
    state_payload = state.state_store.layout.control_executors_path.read_text(
        encoding="utf-8"
    )
    assert first_delivery.credential not in state_payload
    assert started.device_code not in state_payload

    repeated = await service.decide(
        PairApprovalRequest(
            user_code=started.user_code,
            decision=PairDecision.APPROVE,
            name="ignored-on-idempotent-retry",
        )
    )
    assert repeated.executor_id == first_delivery.executor_id
    assert state.snapshot_executors()[first_delivery.executor_id] == record

    await service.aclose()
    await transport.aclose()


@pytest.mark.asyncio
async def test_pair_denial_is_final_and_visible_to_device_poll(
    tmp_path: Path,
) -> None:
    service, state, transport, _clock = _service(tmp_path)
    started = await service.start_pairing(_request())

    denied = await service.decide(
        PairApprovalRequest(
            user_code=started.user_code,
            decision=PairDecision.DENY,
        )
    )
    assert denied.status == "denied"
    assert state.snapshot_executors() == {}

    with pytest.raises(ExecutorPairingError) as caught:
        await service.poll(started.device_code)
    assert caught.value.error.code is ProtocolErrorCode.PAIRING_DENIED

    with pytest.raises(ExecutorPairingError) as changed:
        await service.decide(
            PairApprovalRequest(
                user_code=started.user_code,
                decision=PairDecision.APPROVE,
            )
        )
    assert changed.value.error.code is ProtocolErrorCode.PAIRING_DENIED

    await service.aclose()
    await transport.aclose()


@pytest.mark.asyncio
async def test_owner_replacement_keeps_executor_id_and_invalidates_old_bearer(
    tmp_path: Path,
) -> None:
    service, state, transport, _clock = _service(tmp_path)
    executor_id = new_executor_id()
    old_credential = new_executor_credential()
    original = ExecutorTrustRecord(
        executor_id=executor_id,
        name="old-name",
        credential_verifier=executor_credential_verifier(old_credential),
        created_at=123.0,
    )
    state.put_executor(original)
    started = await service.start_pairing(
        _request(existing_executor_id=executor_id)
    )

    approved = await service.decide(
        PairApprovalRequest(
            user_code=started.user_code,
            decision=PairDecision.APPROVE,
            replace_executor_id=executor_id,
            name="new-name",
        )
    )
    delivery = await service.poll(started.device_code)
    replaced = state.snapshot_executors()[executor_id]

    assert approved.executor_id == executor_id
    assert delivery.executor_id == executor_id
    assert replaced.name == "new-name"
    assert replaced.created_at == original.created_at
    assert replaced.revoked_at is None
    assert not executor_credential_matches(
        old_credential, replaced.credential_verifier
    )
    assert executor_credential_matches(
        delivery.credential, replaced.credential_verifier
    )

    await service.aclose()
    await transport.aclose()


@pytest.mark.asyncio
async def test_first_authenticated_hello_or_close_erases_plaintext_delivery(
    tmp_path: Path,
) -> None:
    service, _state_owner, transport, _clock = _service(tmp_path)
    started = await service.start_pairing(_request())
    approved = await service.decide(
        PairApprovalRequest(
            user_code=started.user_code,
            decision=PairDecision.APPROVE,
        )
    )
    assert approved.executor_id is not None
    await service.poll(started.device_code)

    await service.complete_authenticated_hello(approved.executor_id)
    with pytest.raises(ExecutorPairingError) as caught:
        await service.poll(started.device_code)
    assert caught.value.error.code is ProtocolErrorCode.PAIRING_EXPIRED

    another = await service.start_pairing(_request())
    await service.aclose()
    with pytest.raises(ExecutorPairingError) as after_close:
        await service.poll(another.device_code)
    assert after_close.value.error.code is ProtocolErrorCode.PAIRING_EXPIRED
    await transport.aclose()
