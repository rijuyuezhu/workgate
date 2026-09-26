"""Process-local terminal stream rendezvous owned by control."""

import asyncio
import contextlib
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..protocol.terminal import TERMINAL_STREAM_MAX_FRAME_BYTES

_STREAM_ID_BYTES = 24
_BROWSER_TOKEN_BYTES = 32
_BROWSER_TOKEN_TTL_S = 30.0


class TerminalStreamSocket(Protocol):
    """Minimal WebSocket surface required by the in-memory stream hub."""

    async def receive(self) -> Any: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def send_text(self, data: str) -> None: ...

    async def close(
        self, code: int = 1000, reason: str | None = None
    ) -> None: ...


@dataclass(frozen=True)
class TerminalStreamGrant:
    stream_id: str
    browser_token: str
    expires_at: float


@dataclass
class _TerminalStream:
    stream_id: str
    executor_id: str
    browser_token_sha256: str
    expires_at: float
    admission_marker: int | None = None
    browser: TerminalStreamSocket | None = None
    executor: TerminalStreamSocket | None = None
    browser_claimed: bool = False
    executor_claimed: bool = False
    executor_ready: bool = False
    paired: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    relay_task: asyncio.Task[None] | None = None
    expiry_task: asyncio.Task[None] | None = None
    last_activity: float = field(default_factory=time.monotonic)


class ControlStreamHub:
    """Bounded ephemeral pairing for browser and executor terminal sockets."""

    def __init__(
        self,
        *,
        max_streams: int,
        idle_timeout_s: float,
        browser_token_ttl_s: float = _BROWSER_TOKEN_TTL_S,
        reserve_slot: Callable[[], int | None] | None = None,
        release_slot: Callable[[int], None] | None = None,
    ) -> None:
        self._max_streams = max(1, int(max_streams))
        self._idle_timeout_s = max(0.0, float(idle_timeout_s))
        self._browser_token_ttl_s = max(0.001, float(browser_token_ttl_s))
        self._reserve_slot = reserve_slot
        self._release_slot = release_slot
        self._streams: dict[str, _TerminalStream] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def create(self, executor_id: str) -> TerminalStreamGrant:
        """Allocate one live stream and one short-lived single-use browser bearer."""
        async with self._lock:
            self._require_open()
            if len(self._streams) >= self._max_streams:
                raise RuntimeError(
                    "Too many pending or active terminal streams"
                )
            admission_marker = (
                None if self._reserve_slot is None else self._reserve_slot()
            )
            if self._reserve_slot is not None and admission_marker is None:
                raise RuntimeError("Too many Human UI terminal connections")
            while True:
                stream_id = "stream_" + secrets.token_urlsafe(_STREAM_ID_BYTES)
                if stream_id not in self._streams:
                    break
            token = secrets.token_urlsafe(_BROWSER_TOKEN_BYTES)
            expires_at = time.time() + self._browser_token_ttl_s
            stream = _TerminalStream(
                stream_id=stream_id,
                executor_id=str(executor_id),
                browser_token_sha256=self._token_sha256(token),
                expires_at=expires_at,
                admission_marker=admission_marker,
            )
            self._schedule_expiry_locked(stream, self._browser_token_ttl_s)
            self._streams[stream_id] = stream
            return TerminalStreamGrant(stream_id, token, expires_at)

    async def cancel(self, stream_id: str) -> None:
        await self._close_stream(
            stream_id, code=1011, reason="Terminal attach failed"
        )

    async def browser_expires_at(self, stream_id: str) -> float | None:
        async with self._lock:
            stream = self._streams.get(str(stream_id))
            if stream is None or stream.browser_claimed:
                return None
            if time.time() >= stream.expires_at:
                return None
            return stream.expires_at

    async def claim_browser(
        self,
        stream_id: str,
        token: str,
        socket: TerminalStreamSocket,
    ) -> bool:
        async with self._lock:
            stream = self._streams.get(str(stream_id))
            if (
                stream is None
                or time.time() >= stream.expires_at
                or stream.browser_claimed
                or not secrets.compare_digest(
                    stream.browser_token_sha256,
                    self._token_sha256(str(token)),
                )
            ):
                return False
            stream.browser_claimed = True
            stream.browser_token_sha256 = ""
            stream.browser = socket
            self._maybe_pair_locked(stream)
            return True

    async def claim_executor(
        self,
        stream_id: str,
        executor_id: str,
        socket: TerminalStreamSocket,
    ) -> bool:
        async with self._lock:
            stream = self._streams.get(str(stream_id))
            if (
                stream is None
                or time.time() >= stream.expires_at
                or stream.executor_claimed
                or stream.executor_id != str(executor_id)
            ):
                return False
            stream.executor_claimed = True
            stream.executor = socket
            if not stream.browser_claimed:
                stream.expires_at = time.time() + self._browser_token_ttl_s
                self._schedule_expiry_locked(stream, self._browser_token_ttl_s)
            return True

    async def activate_executor(self, stream_id: str) -> bool:
        """Mark the claimed executor socket handshake-complete and allow relay."""
        async with self._lock:
            stream = self._streams.get(str(stream_id))
            if (
                stream is None
                or not stream.executor_claimed
                or stream.executor is None
                or stream.executor_ready
            ):
                return False
            stream.executor_ready = True
            self._maybe_pair_locked(stream)
            return True

    async def wait_closed(self, stream_id: str) -> None:
        async with self._lock:
            stream = self._streams.get(str(stream_id))
            if stream is None:
                return
            closed = stream.closed
        await closed.wait()

    async def close_executor(self, executor_id: str) -> None:
        async with self._lock:
            stream_ids = [
                stream_id
                for stream_id, stream in self._streams.items()
                if stream.executor_id == str(executor_id)
            ]
        for stream_id in stream_ids:
            await self._close_stream(
                stream_id,
                code=4403,
                reason="Executor credential is no longer valid",
            )

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            stream_ids = tuple(self._streams)
        for stream_id in stream_ids:
            await self._close_stream(
                stream_id,
                code=1012,
                reason="Control runtime restarted",
            )

    def active_count(self) -> int:
        return len(self._streams)

    def expected_executor(self, stream_id: str) -> str | None:
        stream = self._streams.get(str(stream_id))
        return None if stream is None else stream.executor_id

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Terminal stream hub is closed")

    @staticmethod
    def _token_sha256(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _schedule_expiry_locked(
        self, stream: _TerminalStream, delay: float
    ) -> None:
        previous = stream.expiry_task
        if previous is not None:
            previous.cancel()
        stream.expiry_task = asyncio.create_task(
            self._expire_later(stream.stream_id, delay)
        )

    async def _expire_later(self, stream_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._close_stream(
                stream_id,
                code=4408,
                reason="Terminal attach token expired",
            )
        except asyncio.CancelledError:
            raise

    def _maybe_pair_locked(self, stream: _TerminalStream) -> None:
        if (
            stream.browser is None
            or stream.executor is None
            or not stream.executor_ready
            or stream.relay_task is not None
        ):
            return
        if stream.expiry_task is not None:
            stream.expiry_task.cancel()
            stream.expiry_task = None
        stream.paired.set()
        stream.relay_task = asyncio.create_task(self._relay(stream))

    async def _relay(self, stream: _TerminalStream) -> None:
        assert stream.browser is not None
        assert stream.executor is not None
        browser = stream.browser
        executor = stream.executor
        tasks = [
            asyncio.create_task(
                self._relay_direction(stream, browser, executor)
            ),
            asyncio.create_task(
                self._relay_direction(stream, executor, browser)
            ),
        ]
        if self._idle_timeout_s:
            tasks.append(asyncio.create_task(self._idle_watchdog(stream)))
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except Exception:
            pass
        finally:
            await self._close_stream(stream.stream_id)

    async def _idle_watchdog(self, stream: _TerminalStream) -> None:
        while True:
            remaining = self._idle_timeout_s - (
                time.monotonic() - stream.last_activity
            )
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _relay_direction(
        self,
        stream: _TerminalStream,
        source: TerminalStreamSocket,
        destination: TerminalStreamSocket,
    ) -> None:
        while True:
            message = await source.receive()
            if message.get("type") == "websocket.disconnect":
                return
            raw = message.get("bytes")
            text = message.get("text")
            if raw is not None:
                data = bytes(raw)

                if len(data) > TERMINAL_STREAM_MAX_FRAME_BYTES:
                    return
                stream.last_activity = time.monotonic()
                await destination.send_bytes(data)
                continue
            if text is not None:
                value = str(text)
                if len(value.encode("utf-8")) > TERMINAL_STREAM_MAX_FRAME_BYTES:
                    return
                stream.last_activity = time.monotonic()
                await destination.send_text(value)

    async def _close_stream(
        self,
        stream_id: str,
        *,
        code: int = 1000,
        reason: str = "Terminal stream closed",
    ) -> None:
        async with self._lock:
            stream = self._streams.pop(str(stream_id), None)
            if stream is None:
                return
            admission_marker = stream.admission_marker
            expiry_task = stream.expiry_task
            relay_task = stream.relay_task
            stream.expiry_task = None
            stream.relay_task = None
            sockets = tuple(
                socket
                for socket in (stream.browser, stream.executor)
                if socket is not None
            )
            stream.closed.set()
        if admission_marker is not None and self._release_slot is not None:
            self._release_slot(admission_marker)
        current = asyncio.current_task()
        cancelled_tasks: list[asyncio.Task[None]] = []
        if expiry_task is not None and expiry_task is not current:
            expiry_task.cancel()
            cancelled_tasks.append(expiry_task)
        if relay_task is not None and relay_task is not current:
            relay_task.cancel()
            cancelled_tasks.append(relay_task)
        if cancelled_tasks:
            await asyncio.gather(*cancelled_tasks, return_exceptions=True)
        for socket in sockets:
            with contextlib.suppress(Exception):
                await socket.close(code=code, reason=reason[:120])
