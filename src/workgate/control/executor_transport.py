"""Ephemeral ordinary-RPC coordination for executor protocol v1."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from ..protocol.credentials import executor_credential_matches
from ..protocol.errors import ProtocolError, ProtocolErrorCode
from ..protocol.executor import (
    ExecutorCommand,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
)
from ..protocol.ids import new_command_id
from .state import ControlState, ExecutorTrustRecord


class ExecutorTransportError(RuntimeError):
    """One stable executor-protocol failure raised by the control transport."""

    def __init__(self, code: ProtocolErrorCode, message: str) -> None:
        super().__init__(message)
        self.error = ProtocolError(code=code, message=message)


class ExecutorTransportClosedError(RuntimeError):
    """Raised in pending callers when the process-local transport shuts down."""


@dataclass
class _PendingCommand:
    command: ExecutorCommand
    future: asyncio.Future[ExecutorResult]
    state: Literal["queued", "offered"] = "queued"


@dataclass
class _ExecutorChannel:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    queue: deque[str] = field(default_factory=deque)
    pending: dict[str, _PendingCommand] = field(default_factory=dict)
    poll_active: bool = False
    last_activity: float | None = None
    hello: ExecutorHelloRequest | None = None


class ExecutorTransport:
    """Own bounded process-local executor queues, presence, polls, and Futures."""

    def __init__(
        self,
        control_state: ControlState,
        *,
        max_pending_commands: int,
        heartbeat_interval_s: int = 15,
        offline_after_s: int = 60,
        poll_timeout_s: int = 25,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_pending_commands < 1:
            raise ValueError("max_pending_commands must be positive")
        if heartbeat_interval_s < 1 or poll_timeout_s < 1:
            raise ValueError("executor transport timing must be positive")
        if offline_after_s <= heartbeat_interval_s:
            raise ValueError("offline_after_s must exceed heartbeat_interval_s")
        self._control_state = control_state
        self._max_pending_commands = max_pending_commands
        self._heartbeat_interval_s = heartbeat_interval_s
        self._offline_after_s = offline_after_s
        self._poll_timeout_s = poll_timeout_s
        self._clock = clock
        self._channels: dict[str, _ExecutorChannel] = {}
        self._started = False
        self._closed = False

    def start(self) -> None:
        """Open this process-local coordination owner after durable state is loaded."""
        if self._closed:
            raise RuntimeError(
                "ExecutorTransport cannot be restarted after close"
            )
        self._started = True

    async def aclose(self) -> None:
        """Interrupt ordinary RPC waiters and polls without persisting live state."""
        if self._closed:
            return
        self._closed = True
        self._started = False
        for channel in tuple(self._channels.values()):
            async with channel.lock:
                self._interrupt_channel(
                    channel,
                    ExecutorTransportClosedError("executor transport closed"),
                )
                channel.wake.set()

    def _require_running(self) -> None:
        if self._closed or not self._started:
            raise RuntimeError("ExecutorTransport is not running")

    def _channel(self, executor_id: str) -> _ExecutorChannel:
        channel = self._channels.get(executor_id)
        if channel is None:
            channel = _ExecutorChannel()
            self._channels[executor_id] = channel
        return channel

    def _authenticate(self, credential: str) -> ExecutorTrustRecord:
        records = self._control_state.snapshot_executors()
        for record in records.values():
            if not executor_credential_matches(
                credential, record.credential_verifier
            ):
                continue
            if record.revoked_at is not None:
                raise ExecutorTransportError(
                    ProtocolErrorCode.EXECUTOR_REVOKED,
                    "executor credential has been revoked",
                )
            return record
        raise ExecutorTransportError(
            ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
            "executor credential is not recognized",
        )

    def _reauthenticate(self, executor_id: str, credential: str) -> None:
        record = self._control_state.snapshot_executors().get(executor_id)
        if record is None or not executor_credential_matches(
            credential, record.credential_verifier
        ):
            raise ExecutorTransportError(
                ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
                "executor credential is not current",
            )
        if record.revoked_at is not None:
            raise ExecutorTransportError(
                ProtocolErrorCode.EXECUTOR_REVOKED,
                "executor credential has been revoked",
            )

    def _require_target_trusted(self, executor_id: str) -> None:
        record = self._control_state.snapshot_executors().get(executor_id)
        if record is None:
            raise ExecutorTransportError(
                ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
                "executor is not trusted",
            )
        if record.revoked_at is not None:
            raise ExecutorTransportError(
                ProtocolErrorCode.EXECUTOR_REVOKED,
                "executor credential has been revoked",
            )

    def _touch(self, channel: _ExecutorChannel) -> None:
        channel.last_activity = self._clock()

    def _is_online(self, channel: _ExecutorChannel) -> bool:
        last_activity = channel.last_activity
        return (
            last_activity is not None
            and self._clock() - last_activity <= self._offline_after_s
        )

    async def hello(
        self, credential: str, request: ExecutorHelloRequest
    ) -> ExecutorHelloResponse:
        """Authenticate reconnect state and publish one complete live inventory."""
        self._require_running()
        record = self._authenticate(credential)
        channel = self._channel(record.executor_id)
        async with channel.lock:
            self._reauthenticate(record.executor_id, credential)
            channel.hello = request
            self._touch(channel)
        return ExecutorHelloResponse(
            heartbeat_interval_s=self._heartbeat_interval_s,
            offline_after_s=self._offline_after_s,
            poll_timeout_s=self._poll_timeout_s,
        )

    async def heartbeat(self, credential: str) -> None:
        """Refresh authenticated presence independently of command capacity."""
        self._require_running()
        record = self._authenticate(credential)
        channel = self._channel(record.executor_id)
        async with channel.lock:
            self._reauthenticate(record.executor_id, credential)
            self._touch(channel)

    async def poll(self, credential: str) -> ExecutorCommand | None:
        """Long-poll for at most one command, marking it offered exactly once."""
        self._require_running()
        record = self._authenticate(credential)
        executor_id = record.executor_id
        channel = self._channel(executor_id)
        async with channel.lock:
            self._reauthenticate(executor_id, credential)
            if channel.poll_active:
                raise ExecutorTransportError(
                    ProtocolErrorCode.EXECUTOR_OVERLOADED,
                    "executor already has an active delivery poll",
                )
            channel.poll_active = True
            self._touch(channel)

        deadline = self._clock() + self._poll_timeout_s
        try:
            while True:
                async with channel.lock:
                    self._require_running()
                    self._reauthenticate(executor_id, credential)
                    self._touch(channel)
                    command = self._offer_next(channel)
                    if command is not None:
                        return command
                    channel.wake.clear()
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        return None
                try:
                    await asyncio.wait_for(
                        channel.wake.wait(), timeout=remaining
                    )
                except TimeoutError:
                    return None
        finally:
            async with channel.lock:
                channel.poll_active = False

    def _offer_next(self, channel: _ExecutorChannel) -> ExecutorCommand | None:
        while channel.queue:
            command_id = channel.queue.popleft()
            pending = channel.pending.get(command_id)
            if pending is None or pending.state != "queued":
                continue
            pending.state = "offered"
            return pending.command
        return None

    async def call(
        self,
        executor_id: str,
        op: str,
        args: Mapping[str, JsonValue] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
    ) -> ExecutorResult:
        """Queue one ordinary command and await its live, non-durable result."""
        self._require_running()
        channel = self._channel(executor_id)
        async with channel.lock:
            self._require_target_trusted(executor_id)
            if not self._is_online(channel):
                raise ExecutorTransportError(
                    ProtocolErrorCode.EXECUTOR_OFFLINE,
                    "executor is offline",
                )
            if len(channel.pending) >= self._max_pending_commands:
                raise ExecutorTransportError(
                    ProtocolErrorCode.EXECUTOR_OVERLOADED,
                    "executor command capacity is full",
                )
            command = ExecutorCommand(
                id=new_command_id(),
                op=op,
                session_id=session_id,
                args=dict(args or {}),
            )
            future = asyncio.get_running_loop().create_future()
            pending = _PendingCommand(command=command, future=future)
            channel.pending[command.id] = pending
            channel.queue.append(command.id)
            channel.wake.set()

        try:
            waiter = asyncio.shield(future)
            if timeout_s is None:
                return await waiter
            async with asyncio.timeout(timeout_s):
                return await waiter
        except asyncio.CancelledError, TimeoutError:
            await self._abandon(executor_id, command.id)
            raise

    async def _abandon(self, executor_id: str, command_id: str) -> None:
        channel = self._channels.get(executor_id)
        if channel is None:
            return
        async with channel.lock:
            pending = channel.pending.pop(command_id, None)
            if pending is None:
                return
            if pending.state == "queued":
                with contextlib.suppress(ValueError):
                    channel.queue.remove(command_id)
            if not pending.future.done():
                pending.future.cancel()

    async def submit_result(
        self, credential: str, result: ExecutorResult
    ) -> None:
        """Accept a result only for this current bearer and one live offered ID."""
        self._require_running()
        record = self._authenticate(credential)
        channel = self._channel(record.executor_id)
        async with channel.lock:
            self._reauthenticate(record.executor_id, credential)
            self._touch(channel)
            pending = channel.pending.get(result.id)
            if pending is None or pending.state != "offered":
                raise ExecutorTransportError(
                    ProtocolErrorCode.UNKNOWN_COMMAND,
                    "command is not awaiting a result",
                )
            channel.pending.pop(result.id, None)
            if not pending.future.done():
                pending.future.set_result(result)

    async def is_online(self, executor_id: str) -> bool:
        """Return process-local presence derived from recent authenticated activity."""
        channel = self._channels.get(executor_id)
        if channel is None:
            return False
        async with channel.lock:
            return self._is_online(channel)

    async def pending_count(self, executor_id: str) -> int:
        """Return retained queued plus offered/result correlations for diagnostics."""
        channel = self._channels.get(executor_id)
        if channel is None:
            return 0
        async with channel.lock:
            return len(channel.pending)

    async def inventory(self, executor_id: str) -> ExecutorHelloRequest | None:
        """Return the latest complete process-local reconnect inventory."""
        channel = self._channels.get(executor_id)
        if channel is None:
            return None
        async with channel.lock:
            return channel.hello

    async def revoke_executor(
        self, executor_id: str, *, revoked_at: float
    ) -> ExecutorTrustRecord:
        """Persist revocation under the same handoff lock and fence queued work."""
        self._require_running()
        channel = self._channel(executor_id)
        async with channel.lock:
            record = self._control_state.revoke_executor(
                executor_id, revoked_at=revoked_at
            )
            self._interrupt_channel(
                channel,
                ExecutorTransportError(
                    ProtocolErrorCode.EXECUTOR_REVOKED,
                    "executor credential has been revoked",
                ),
            )
            channel.last_activity = None
            channel.hello = None
            channel.wake.set()
            return record

    async def replace_executor(self, record: ExecutorTrustRecord) -> None:
        """Replace trust under the handoff lock and interrupt old live correlations."""
        self._require_running()
        channel = self._channel(record.executor_id)
        async with channel.lock:
            self._control_state.put_executor(record)
            self._interrupt_channel(
                channel,
                ExecutorTransportError(
                    ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
                    "executor credential was replaced",
                ),
            )
            channel.last_activity = None
            channel.hello = None
            channel.wake.set()

    @staticmethod
    def _interrupt_channel(
        channel: _ExecutorChannel, exc: BaseException
    ) -> None:
        channel.queue.clear()
        pending = tuple(channel.pending.values())
        channel.pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_exception(exc)
