"""One-shot, executor-owned overlay receivers for uncommitted transfer bytes.

This is private preparation for the peer-direct route. A receiver cannot publish
the destination: Control must use the existing transfer_finish_write receipt path.
"""

import asyncio
import contextlib
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field

from ..config.executor import ExecutorConfig
from ..persistence import use_state_store
from .overlay import detect_tailscale_overlay
from .tool_session.store import ToolSessionStore
from .transfer import (
    TransferContext,
    transfer_abort_write,
    transfer_begin_write,
    transfer_write_bytes,
)

_MAX_RECEIVERS = 4
_MAX_CONNECTIONS_PER_RECEIVER = 4
_MAX_TRANSFER_BYTES = 4 * 1024 * 1024 * 1024
_RECEIVER_LIFETIME_S = 120
_STREAM_TIMEOUT_S = 120
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
    server: asyncio.AbstractServer
    state: str = "pending"
    task: asyncio.Task[None] | None = None
    expiry: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_connections: int = 0


class PeerReceiverManager:
    """Bounded live listeners, owned by an ExecutorRuntime, never a daemon."""

    def __init__(self, config: ExecutorConfig, store: ToolSessionStore) -> None:
        self._store = store
        self._context = TransferContext(config, store)
        self._leases: dict[str, _Lease] = {}
        self._mutation_lock = asyncio.Lock()
        self._closed = False

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
        # Serialize preparation with session invalidation and shutdown, so
        # a terminated session can never publish a new listener afterward.
        async with self._mutation_lock:
            return await self._prepare_bound(
                session_id=session_id,
                path=path,
                transfer_id=transfer_id,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                overwrite=overwrite,
                tailnet_id=tailnet_id,
            )

    async def _prepare_bound(
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
        """Open a one-use capability, after reserving a bound write transaction."""
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
        # The disk transaction must settle before cancellation can release the
        # session lock, otherwise it could appear *after* session termination.
        with use_state_store(self._store.state_store):
            begin_task = asyncio.create_task(
                asyncio.to_thread(
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
            begun = await asyncio.shield(begin_task)
        except BaseException:
            with contextlib.suppress(BaseException):
                await begin_task
            await self._abort(session_id, path, transfer_id)
            raise
        if begun.offset or begun.completed:
            raise RuntimeError(
                "peer receiver requires an empty uncommitted write"
            )
        token = secrets.token_urlsafe(32)
        lease: _Lease | None = None

        async def accept(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            assert lease is not None
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
            sock = server.sockets
            if not sock:
                raise RuntimeError("peer receiver did not bind")
            lease = _Lease(
                session_id,
                transfer_id,
                path,
                expected_bytes,
                expected_sha256,
                token,
                server,
            )
            self._leases[transfer_id] = lease
            await server.start_serving()
            lease.expiry = asyncio.create_task(self._expire(lease))
            return {
                "host": overlay.ipv4,
                "port": int(sock[0].getsockname()[1]),
                "token": token,
                "transfer_id": transfer_id,
            }
        except BaseException:
            if lease is not None:
                lease.server.close()
                await lease.server.wait_closed()
                self._leases.pop(transfer_id, None)
            await self._abort(session_id, path, transfer_id)
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
                lease.server.close()
            digest = hashlib.sha256()
            received = 0
            try:
                async with asyncio.timeout(_STREAM_TIMEOUT_S):
                    while received < lease.expected_bytes:
                        self._store.admit_active_session(lease.session_id)
                        block = await reader.read(
                            min(_CHUNK_SIZE, lease.expected_bytes - received)
                        )
                        if not block:
                            raise EOFError("peer transfer truncated")
                        digest.update(block)
                        with use_state_store(self._store.state_store):
                            await asyncio.to_thread(
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
                await self._abort(
                    lease.session_id, lease.path, lease.transfer_id
                )
                raise
        except Exception, asyncio.CancelledError:
            # Never return a capability or raw payload in the error.
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _expire(self, lease: _Lease) -> None:
        try:
            await asyncio.sleep(_RECEIVER_LIFETIME_S)
            await self.cancel(lease.transfer_id)
        except asyncio.CancelledError:
            return

    async def _abort(
        self, session_id: str, path: str, transfer_id: str
    ) -> None:
        with (
            contextlib.suppress(Exception),
            use_state_store(self._store.state_store),
        ):
            await asyncio.to_thread(
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
        lease = self._leases.get(transfer_id)
        if lease is None or (
            session_id is not None and lease.session_id != session_id
        ):
            return False
        self._leases.pop(transfer_id)
        lease.state = "failed" if lease.state != "received" else lease.state
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
        # A committed receipt is preserved by transfer_abort_write; an incomplete
        # or complete-but-unpublished transfer is safely discarded.
        await self._abort(lease.session_id, lease.path, lease.transfer_id)
        return True

    async def cancel_session(self, session_id: str) -> None:
        async with self._mutation_lock:
            for transfer_id, lease in tuple(self._leases.items()):
                if lease.session_id == session_id:
                    await self.cancel(transfer_id)

    async def aclose(self) -> None:
        async with self._mutation_lock:
            self._closed = True
            for transfer_id in tuple(self._leases):
                await self.cancel(transfer_id)
