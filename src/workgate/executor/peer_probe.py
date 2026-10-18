"""Sessionless, one-shot Tailscale peer connectivity proof; no file traffic."""

import asyncio
import hmac
import secrets
from dataclasses import dataclass
from typing import TypedDict

from .overlay import detect_tailscale_overlay
from .peer_sender import _validate_endpoint

_LISTEN_SECONDS = 12
_CONNECT_SECONDS = 4
_MAX_PROBES = 2


class PeerProbeEndpoint(TypedDict):
    host: str
    port: int
    token: str
    probe_id: str


@dataclass
class _PendingProbe:
    token: str
    server: asyncio.AbstractServer
    expiry: asyncio.Task[None]


class PeerProbeManager:
    """Executor-owned bounded ephemeral listeners. Nothing survives a restart."""

    def __init__(self) -> None:
        self._probes: dict[str, _PendingProbe] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def prepare(self, tailnet_id: str) -> PeerProbeEndpoint:
        async with self._lock:
            if self._closed or len(self._probes) >= _MAX_PROBES:
                raise RuntimeError("peer probe is unavailable or at capacity")
            overlay = await asyncio.to_thread(detect_tailscale_overlay)
            if overlay is None or overlay.tailnet_id != tailnet_id:
                raise RuntimeError("trusted overlay is unavailable or changed")

            probe_id = "probe_" + secrets.token_urlsafe(18)
            token = secrets.token_urlsafe(32)
            accepted = False
            active_connections = 0

            async def accept(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                nonlocal accepted, active_connections
                if accepted or active_connections >= 4:
                    writer.close()
                    return
                active_connections += 1
                try:
                    async with asyncio.timeout(_CONNECT_SECONDS):
                        line = await reader.readline()
                        if (
                            len(line) != 44
                            or not hmac.compare_digest(
                                line, token.encode("ascii") + b"\n"
                            )
                            or accepted
                        ):
                            return
                        accepted = True
                        writer.write(b"OK\n")
                        await writer.drain()
                        asyncio.create_task(self.cancel(probe_id))
                except TimeoutError, ConnectionError, OSError, ValueError:
                    pass
                finally:
                    active_connections -= 1
                    writer.close()
                    try:
                        async with asyncio.timeout(1):
                            await writer.wait_closed()
                    except TimeoutError, OSError:
                        writer.transport.abort()

            server = await asyncio.start_server(
                accept, overlay.ipv4, 0, limit=128
            )
            if not server.sockets:
                server.close()
                await server.wait_closed()
                raise RuntimeError("peer probe failed to bind")
            port = int(server.sockets[0].getsockname()[1])

            async def expire() -> None:
                try:
                    await asyncio.sleep(_LISTEN_SECONDS)
                    await self.cancel(probe_id)
                except asyncio.CancelledError:
                    pass

            expiry = asyncio.create_task(expire())
            self._probes[probe_id] = _PendingProbe(token, server, expiry)
            return {
                "host": overlay.ipv4,
                "port": port,
                "token": token,
                "probe_id": probe_id,
            }

    async def cancel(self, probe_id: str) -> None:
        async with self._lock:
            lease = self._probes.pop(probe_id, None)
            if lease is None:
                return
            if lease.expiry is not asyncio.current_task():
                lease.expiry.cancel()
            lease.server.close()
            await lease.server.wait_closed()

    async def aclose(self) -> None:
        self._closed = True
        for probe_id in tuple(self._probes):
            await self.cancel(probe_id)


async def connect_peer_probe(
    *,
    host: str,
    port: int,
    token: str,
    tailnet_id: str,
) -> dict[str, bool]:
    """Verify the receiver's one-shot capability on the real overlay TCP path."""
    _validate_endpoint(host, port, token)
    overlay = await asyncio.to_thread(detect_tailscale_overlay)
    if overlay is None or overlay.tailnet_id != tailnet_id:
        raise RuntimeError("trusted overlay is unavailable or changed")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=_CONNECT_SECONDS,
        )
    except (OSError, TimeoutError) as exc:
        raise RuntimeError("peer connection is unreachable") from exc
    try:
        async with asyncio.timeout(_CONNECT_SECONDS):
            writer.write(token.encode("ascii") + b"\n")
            await writer.drain()
            if await reader.readline() != b"OK\n":
                raise RuntimeError("peer handshake rejected")
        return {"connected": True}
    except (OSError, TimeoutError) as exc:
        raise RuntimeError("peer handshake rejected") from exc
    finally:
        writer.close()
        try:
            async with asyncio.timeout(1):
                await writer.wait_closed()
        except TimeoutError, OSError:
            writer.transport.abort()
