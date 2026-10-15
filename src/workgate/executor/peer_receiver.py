"""One-use, executor-owned overlay receivers for uncommitted transfer bytes.

Control alone publishes files using the existing transfer_finish_write receipt.
"""

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from ..config.executor import ExecutorConfig
from ..persistence import use_state_store
from .overlay import detect_tailscale_overlay
from .tool_session.store import ToolSessionStore
from .transfer import (
    TransferContext,
    _write_receipt_path,
    transfer_abort_write,
    transfer_begin_write,
    transfer_write_bytes,
)

logger = logging.getLogger(__name__)
_MAX_RECEIVERS = 4
_MAX_CONNECTIONS_PER_RECEIVER = 4
_MAX_TRANSFER_BYTES = 4 * 1024 * 1024 * 1024
_CONNECT_TIMEOUT_S = 120
_IDLE_TIMEOUT_S = 60
_RECEIPT_GRACE_S = 120
_AUTH_TIMEOUT_S = 5
_CHUNK_SIZE = 256 * 1024


@dataclass
class _Lease:
    session_id: str
    transfer_id: str
    path: str
    expected_bytes: int
    expected_sha256: str
    token: str
    server: asyncio.AbstractServer | None
    state: str = "pending"
    task: asyncio.Task[None] | None = None
    expiry: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_connections: int = 0


class PeerReceiverManager:
    """Live listeners with session/transfer serialization and retryable cleanup."""

    def __init__(self, config: ExecutorConfig, store: ToolSessionStore) -> None:
        self._store = store
        self._context = TransferContext(config, store)
        self._leases: dict[str, _Lease] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def _io(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        # asyncio.to_thread cannot be interrupted once a disk mutation starts.
        # Await its completion before a cancelled receiver can release the lock.
        with use_state_store(self._store.state_store):
            task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await task
            raise

    @asynccontextmanager
    async def session_lifecycle(self, session_id: str) -> AsyncGenerator[None]:
        """Keep session mutation serialized with preparation through completion."""
        async with self._lock:
            await self._cancel_session_locked(session_id)
            yield

    async def prepare(
        self,
        *,
        session_id: str,
        path: str,
        transfer_id: str,
        expected_bytes: int,
        expected_sha256: str,
        overwrite: bool,
        tailnet_id: str,
    ) -> dict[str, str | int]:
        """Reserve a bound transaction and open a one-use overlay listener."""
        async with self._lock:
            if self._closed or len(self._leases) >= _MAX_RECEIVERS:
                raise RuntimeError("peer receiver unavailable or at capacity")
            if not (0 <= expected_bytes <= _MAX_TRANSFER_BYTES):
                raise ValueError("invalid peer transfer size")
            if len(expected_sha256) != 64 or any(
                char not in "0123456789abcdef" for char in expected_sha256
            ):
                raise ValueError("invalid peer transfer digest")
            if (
                len(transfer_id) > 128
                or re.fullmatch(r"copy_[A-Za-z0-9_-]{22,}", transfer_id) is None
            ):
                raise ValueError("invalid peer transfer identity")
            if transfer_id in self._leases:
                raise ValueError("peer receiver already exists for transfer")
            self._store.admit_active_session(session_id)
            overlay = await asyncio.to_thread(detect_tailscale_overlay)
            if overlay is None or overlay.tailnet_id != tailnet_id:
                raise RuntimeError("trusted overlay is unavailable or changed")
            # A peer receiver owns only a *new* transaction. Never roll back an
            # existing receipt, including one with offset zero.
            if _write_receipt_path(self._context, transfer_id).exists():
                raise ValueError(
                    "peer receiver requires a new transfer identity"
                )
            # The disk thread must settle even when this RPC is cancelled.
            beginning = asyncio.create_task(
                self._io(
                    transfer_begin_write,
                    path,
                    overwrite,
                    expected_bytes,
                    transfer_id,
                    session_id=session_id,
                    context=self._context,
                )
            )
            try:
                begun = await asyncio.shield(beginning)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    with contextlib.suppress(Exception):
                        await beginning
                # Begin may have written a receipt before throwing. We proved
                # this transfer identity was unowned before calling Begin.
                try:
                    await self._abort(session_id, path, transfer_id)
                except Exception:
                    logger.exception("peer begin cleanup failed")
                    self._leases[transfer_id] = _Lease(
                        session_id,
                        transfer_id,
                        path,
                        expected_bytes,
                        expected_sha256,
                        "",
                        None,
                        state="cleanup_failed",
                    )
                raise
            if begun.offset or begun.completed or begun.resumed:
                raise RuntimeError(
                    "peer receiver requires a new uncommitted write"
                )
            lease = _Lease(
                session_id,
                transfer_id,
                path,
                expected_bytes,
                expected_sha256,
                secrets.token_urlsafe(32),
                None,
            )
            self._leases[transfer_id] = lease

            async def accept(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                if lease.active_connections >= _MAX_CONNECTIONS_PER_RECEIVER:
                    writer.close()
                    return
                lease.active_connections += 1
                try:
                    await self._receive(lease, reader, writer)
                finally:
                    lease.active_connections -= 1

            try:
                server = await asyncio.start_server(
                    accept, overlay.ipv4, 0, limit=256, start_serving=False
                )
                if not server.sockets:
                    raise RuntimeError("peer receiver did not bind")
                port = int(server.sockets[0].getsockname()[1])
                lease.server = server
                await server.start_serving()
                lease.expiry = asyncio.create_task(self._expire(lease))
                return {
                    "host": overlay.ipv4,
                    "port": port,
                    "token": lease.token,
                    "transfer_id": transfer_id,
                }
            except BaseException:
                if lease.server is not None:
                    lease.server.close()
                    await lease.server.wait_closed()
                try:
                    await self._abort(session_id, path, transfer_id)
                except Exception:
                    lease.state = "cleanup_failed"
                    logger.exception("peer prepare cleanup failed")
                    raise
                self._leases.pop(transfer_id, None)
                raise

    async def _receive(
        self,
        lease: _Lease,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                async with asyncio.timeout(_AUTH_TIMEOUT_S):
                    supplied = await reader.readline()
            except TimeoutError, ValueError:
                return
            if len(supplied) > 128 or not hmac.compare_digest(
                supplied.rstrip(b"\n"), lease.token.encode("ascii")
            ):
                return
            async with lease.lock:
                if lease.state != "pending":
                    return
                lease.state = "receiving"
                lease.task = asyncio.current_task()
                assert lease.server is not None
                lease.server.close()
            digest = hashlib.sha256()
            received = 0
            try:
                while received < lease.expected_bytes:
                    self._store.admit_active_session(lease.session_id)
                    async with asyncio.timeout(_IDLE_TIMEOUT_S):
                        block = await reader.read(
                            min(_CHUNK_SIZE, lease.expected_bytes - received)
                        )
                    if not block:
                        raise EOFError("peer transfer truncated")
                    digest.update(block)
                    await self._io(
                        transfer_write_bytes,
                        lease.path,
                        lease.transfer_id,
                        received,
                        block,
                        session_id=lease.session_id,
                        context=self._context,
                    )
                    received += len(block)
                if digest.hexdigest() != lease.expected_sha256:
                    raise ValueError("peer transfer digest mismatch")
                lease.state = "received"
                writer.write(b"OK\n")
                await writer.drain()
            except Exception, asyncio.CancelledError:
                lease.state = "failed"
                try:
                    await self._abort(
                        lease.session_id, lease.path, lease.transfer_id
                    )
                except Exception:
                    lease.state = "cleanup_failed"
                    logger.exception("peer receive cleanup failed")
                raise
        except Exception, asyncio.CancelledError:
            # The peer must not see transaction internals.
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _expire(self, lease: _Lease) -> None:
        try:
            await asyncio.sleep(_CONNECT_TIMEOUT_S)
            # Once the peer has claimed the listener, progress is governed by
            # idle timeout, not an arbitrary total transfer duration.
            while lease.state == "receiving":
                await asyncio.sleep(1)
            if lease.state == "received":
                await asyncio.sleep(_RECEIPT_GRACE_S)
            await self.cancel(lease.transfer_id)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("peer expiry cleanup failed")

    async def _abort(
        self, session_id: str, path: str, transfer_id: str
    ) -> None:
        await self._io(
            transfer_abort_write,
            path,
            transfer_id,
            session_id=session_id,
            context=self._context,
        )

    def status(self, transfer_id: str, *, session_id: str | None = None) -> str:
        lease = self._leases.get(transfer_id)
        if lease is None or (
            session_id is not None and lease.session_id != session_id
        ):
            return "unknown"
        return lease.state

    async def cancel(
        self, transfer_id: str, *, session_id: str | None = None
    ) -> bool:
        async with self._lock:
            return await self._cancel_locked(transfer_id, session_id=session_id)

    async def _cancel_locked(
        self, transfer_id: str, *, session_id: str | None = None
    ) -> bool:
        lease = self._leases.get(transfer_id)
        if lease is None or (
            session_id is not None and lease.session_id != session_id
        ):
            return False
        # An already connected, not-yet-authenticated peer must not claim this
        # lease while the listener and its write receipt are being discarded.
        lease.state = "cancelling"
        if lease.server is not None:
            lease.server.close()
            await lease.server.wait_closed()
        current = asyncio.current_task()
        if lease.expiry is not None and lease.expiry is not current:
            lease.expiry.cancel()
        if (
            lease.task is not None
            and lease.task is not current
            and not lease.task.done()
        ):
            lease.task.cancel()
            await asyncio.gather(lease.task, return_exceptions=True)
        try:
            await self._abort(lease.session_id, lease.path, transfer_id)
        except Exception:
            lease.state = "cleanup_failed"
            logger.exception("peer cancel cleanup failed")
            raise
        self._leases.pop(transfer_id, None)
        return True

    async def _cancel_many_locked(self, transfer_ids: tuple[str, ...]) -> None:
        # Immediately revoke every lease and close every listener before any
        # potentially failing disk cleanup can interrupt shutdown.
        for transfer_id in transfer_ids:
            lease = self._leases.get(transfer_id)
            if lease is not None:
                lease.state = "cancelling"
                if lease.server is not None:
                    lease.server.close()
        errors: list[Exception] = []
        for transfer_id in transfer_ids:
            try:
                await self._cancel_locked(transfer_id)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("peer receiver cleanup failed", errors)

    async def _cancel_session_locked(self, session_id: str) -> None:
        await self._cancel_many_locked(
            tuple(
                transfer_id
                for transfer_id, lease in self._leases.items()
                if lease.session_id == session_id
            )
        )

    async def cancel_session(self, session_id: str) -> None:
        async with self._lock:
            await self._cancel_session_locked(session_id)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._cancel_many_locked(tuple(self._leases))
