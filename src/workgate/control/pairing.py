"""Process-local device-code pairing for final executors."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ..protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from ..protocol.errors import ProtocolError, ProtocolErrorCode
from ..protocol.ids import new_device_code, new_executor_id, new_user_code
from ..protocol.pairing import (
    PairApprovalRequest,
    PairDecision,
    PairingExecutorMetadata,
    PairPollSuccess,
    PairStartRequest,
    PairStartResponse,
)
from .state import ControlState, ExecutorTrustRecord

if TYPE_CHECKING:
    from .executor_transport import ExecutorTransport


_RATE_WINDOW_S = 60.0
_DEFAULT_START_RATE_LIMIT = 30
_DEFAULT_USER_CODE_RATE_LIMIT = 60
_DEFAULT_POLL_INTERVAL_S = 2


class ExecutorPairingError(RuntimeError):
    """One stable pairing failure surfaced through protocol/owner routes."""

    def __init__(self, code: ProtocolErrorCode, message: str) -> None:
        super().__init__(message)
        self.error = ProtocolError(code=code, message=message)


@dataclass(frozen=True, slots=True)
class PairingAttemptView:
    """Secret-free owner view of one live pairing attempt."""

    user_code: str
    requested_name: str | None
    existing_executor_id: str | None
    metadata: PairingExecutorMetadata
    expires_in: int
    status: Literal["pending", "approved", "denied"]
    executor_id: str | None = None
    name: str | None = None


@dataclass(slots=True)
class _PairingAttempt:
    request: PairStartRequest
    user_code: str
    expires_at: float
    device_code: str = field(repr=False)
    next_poll_at: float = 0.0
    decision: PairDecision | None = None
    executor_id: str | None = None
    name: str | None = None
    credential: str | None = field(default=None, repr=False)


class ExecutorPairingService:
    """Own bounded ephemeral pairing attempts and one-shot trust issuance."""

    def __init__(
        self,
        control_state: ControlState,
        transport: ExecutorTransport,
        *,
        verification_uri: str,
        max_pending_attempts: int,
        ttl_s: int,
        poll_interval_s: int = _DEFAULT_POLL_INTERVAL_S,
        start_rate_limit: int = _DEFAULT_START_RATE_LIMIT,
        user_code_rate_limit: int = _DEFAULT_USER_CODE_RATE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if max_pending_attempts < 1:
            raise ValueError("max_pending_attempts must be positive")
        if ttl_s < 1 or poll_interval_s < 1:
            raise ValueError("pairing timing must be positive")
        if start_rate_limit < 1 or user_code_rate_limit < 1:
            raise ValueError("pairing rate limits must be positive")
        self._control_state = control_state
        self._transport = transport
        self._verification_uri = verification_uri
        self._max_pending_attempts = max_pending_attempts
        self._ttl_s = ttl_s
        self._poll_interval_s = poll_interval_s
        self._start_rate_limit = start_rate_limit
        self._user_code_rate_limit = user_code_rate_limit
        self._clock = clock
        self._wall_clock = wall_clock
        self._attempts: dict[str, _PairingAttempt] = {}
        self._device_by_user_code: dict[str, str] = {}
        self._start_events: deque[float] = deque()
        self._user_code_events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def start_pairing(
        self, request: PairStartRequest
    ) -> PairStartResponse:
        """Admit one bounded unauthenticated device-code pairing attempt."""
        async with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            self._admit_rate_locked(
                self._start_events,
                now,
                self._start_rate_limit,
                "pairing start rate limit exceeded",
            )
            if len(self._attempts) >= self._max_pending_attempts:
                raise ExecutorPairingError(
                    ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED,
                    "pairing attempt capacity is exhausted",
                )

            device_code = self._unique_device_code_locked()
            user_code = self._unique_user_code_locked()
            self._attempts[device_code] = _PairingAttempt(
                request=request,
                device_code=device_code,
                user_code=user_code,
                expires_at=now + self._ttl_s,
            )
            self._device_by_user_code[user_code] = device_code
            return PairStartResponse(
                device_code=device_code,
                user_code=user_code,
                verification_uri=self._verification_uri,
                expires_in=self._ttl_s,
                poll_interval=self._poll_interval_s,
            )

    async def lookup_user_code(self, user_code: str) -> PairingAttemptView:
        """Return an owner-safe pending-attempt view after bounded code lookup."""
        async with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            self._admit_rate_locked(
                self._user_code_events,
                now,
                self._user_code_rate_limit,
                "pairing user-code rate limit exceeded",
            )
            attempt = self._attempt_for_user_code_locked(user_code)
            return self._view_locked(attempt, now)

    async def decide(self, request: PairApprovalRequest) -> PairingAttemptView:
        """Apply one authenticated owner approval/denial decision exactly once."""
        async with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            attempt = self._attempt_for_user_code_locked(request.user_code)
            if attempt.decision is not None:
                if attempt.decision is request.decision:
                    return self._view_locked(attempt, now)
                raise ExecutorPairingError(
                    ProtocolErrorCode.PAIRING_DENIED,
                    "pairing attempt already has a final owner decision",
                )

            if request.decision is PairDecision.DENY:
                attempt.decision = PairDecision.DENY
                return self._view_locked(attempt, now)

            credential = new_executor_credential()
            records = self._control_state.snapshot_executors()
            replace_id = request.replace_executor_id
            if replace_id is None:
                executor_id = self._new_executor_id_locked(records)
                name = self._approved_name(request, attempt)
                record = ExecutorTrustRecord(
                    executor_id=executor_id,
                    name=name,
                    credential_verifier=executor_credential_verifier(
                        credential
                    ),
                    created_at=self._wall_clock(),
                )
                self._control_state.put_executor(record)
            else:
                current = records.get(replace_id)
                if current is None:
                    raise ExecutorPairingError(
                        ProtocolErrorCode.PAIRING_REQUIRED,
                        "replacement executor does not exist",
                    )
                executor_id = current.executor_id
                name = request.name or current.name
                record = current.model_copy(
                    update={
                        "name": name,
                        "credential_verifier": executor_credential_verifier(
                            credential
                        ),
                        "revoked_at": None,
                    }
                )
                await self._transport.replace_executor(record)

            self._drop_other_delivery_locked(
                executor_id, keep_device_code=attempt.device_code
            )
            attempt.decision = PairDecision.APPROVE
            attempt.executor_id = executor_id
            attempt.name = name
            attempt.credential = credential
            return self._view_locked(attempt, now)

    async def poll(self, device_code: str) -> PairPollSuccess:
        """Retry credential delivery without regenerating or consuming the bearer."""
        async with self._lock:
            now = self._clock()
            self._prune_expired_locked(now)
            attempt = self._attempts.get(device_code)
            if attempt is None:
                raise ExecutorPairingError(
                    ProtocolErrorCode.PAIRING_EXPIRED,
                    "pairing attempt is unavailable or expired",
                )
            if now < attempt.next_poll_at:
                raise ExecutorPairingError(
                    ProtocolErrorCode.PAIRING_PENDING,
                    "pairing approval is pending",
                )
            attempt.next_poll_at = now + self._poll_interval_s
            if attempt.decision is None:
                raise ExecutorPairingError(
                    ProtocolErrorCode.PAIRING_PENDING,
                    "pairing approval is pending",
                )
            if attempt.decision is PairDecision.DENY:
                raise ExecutorPairingError(
                    ProtocolErrorCode.PAIRING_DENIED,
                    "pairing request was denied by the owner",
                )
            if attempt.executor_id is None or attempt.credential is None:
                raise ExecutorPairingError(
                    ProtocolErrorCode.PAIRING_EXPIRED,
                    "pairing credential delivery is no longer available",
                )
            return PairPollSuccess(
                executor_id=attempt.executor_id,
                credential=attempt.credential,
            )

    async def complete_authenticated_hello(self, executor_id: str) -> None:
        """Erase transient plaintext delivery after the bearer authenticates once."""
        async with self._lock:
            self._prune_expired_locked(self._clock())
            for device_code, attempt in tuple(self._attempts.items()):
                if (
                    attempt.executor_id == executor_id
                    and attempt.credential is not None
                ):
                    self._remove_attempt_locked(device_code)

    async def clear_executor_delivery(self, executor_id: str) -> None:
        """Erase undelivered plaintext after owner revocation/replacement."""
        await self.complete_authenticated_hello(executor_id)

    async def aclose(self) -> None:
        """Discard all pairing secrets; no attempt survives control restart."""
        async with self._lock:
            self._attempts.clear()
            self._device_by_user_code.clear()
            self._start_events.clear()
            self._user_code_events.clear()

    async def attempt_count(self) -> int:
        """Return current live attempt count for tests/diagnostics."""
        async with self._lock:
            self._prune_expired_locked(self._clock())
            return len(self._attempts)

    def _prune_expired_locked(self, now: float) -> None:
        for device_code, attempt in tuple(self._attempts.items()):
            if attempt.expires_at <= now:
                self._remove_attempt_locked(device_code)

    def _remove_attempt_locked(self, device_code: str) -> None:
        attempt = self._attempts.pop(device_code, None)
        if attempt is not None:
            self._device_by_user_code.pop(attempt.user_code, None)

    def _drop_other_delivery_locked(
        self, executor_id: str, *, keep_device_code: str
    ) -> None:
        for device_code, attempt in tuple(self._attempts.items()):
            if (
                device_code != keep_device_code
                and attempt.executor_id == executor_id
                and attempt.credential is not None
            ):
                self._remove_attempt_locked(device_code)

    def _attempt_for_user_code_locked(self, user_code: str) -> _PairingAttempt:
        device_code = self._device_by_user_code.get(user_code)
        attempt = (
            None if device_code is None else self._attempts.get(device_code)
        )
        if attempt is None:
            raise ExecutorPairingError(
                ProtocolErrorCode.PAIRING_REQUIRED,
                "pairing user code is invalid or expired",
            )
        return attempt

    def _view_locked(
        self, attempt: _PairingAttempt, now: float
    ) -> PairingAttemptView:
        status: Literal["pending", "approved", "denied"]
        if attempt.decision is PairDecision.APPROVE:
            status = "approved"
        elif attempt.decision is PairDecision.DENY:
            status = "denied"
        else:
            status = "pending"
        return PairingAttemptView(
            user_code=attempt.user_code,
            requested_name=attempt.request.requested_name,
            existing_executor_id=attempt.request.existing_executor_id,
            metadata=attempt.request.metadata,
            expires_in=max(0, math.ceil(attempt.expires_at - now)),
            status=status,
            executor_id=attempt.executor_id,
            name=attempt.name,
        )

    def _admit_rate_locked(
        self,
        events: deque[float],
        now: float,
        limit: int,
        message: str,
    ) -> None:
        cutoff = now - _RATE_WINDOW_S
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= limit:
            raise ExecutorPairingError(
                ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED,
                message,
            )
        events.append(now)

    def _unique_device_code_locked(self) -> str:
        while True:
            candidate = new_device_code()
            if candidate not in self._attempts:
                return candidate

    def _unique_user_code_locked(self) -> str:
        while True:
            candidate = new_user_code()
            if candidate not in self._device_by_user_code:
                return candidate

    @staticmethod
    def _new_executor_id_locked(
        records: Mapping[str, ExecutorTrustRecord],
    ) -> str:
        while True:
            candidate = new_executor_id()
            if candidate not in records:
                return candidate

    @staticmethod
    def _approved_name(
        request: PairApprovalRequest, attempt: _PairingAttempt
    ) -> str:
        return (
            request.name
            or attempt.request.requested_name
            or attempt.request.metadata.hostname
            or "executor"
        )
