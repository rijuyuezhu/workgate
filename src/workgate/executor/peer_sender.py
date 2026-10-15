"""One-shot trusted-overlay raw byte sender for executor-to-executor transfers."""

import asyncio
import contextlib
import ipaddress
import re

from ..config.executor import ExecutorConfig
from ..protocol.transfer import normalize_chunk_size
from .overlay import detect_tailscale_overlay
from .tool_session.store import ToolSessionStore
from .transfer_http import _open_source, _source_content

_CONNECT_TIMEOUT_S = 5
_IO_TIMEOUT_S = 60
_MAX_TRANSFER_BYTES = 4 * 1024 * 1024 * 1024
_TAILSCALE_IPV4 = ipaddress.IPv4Network("100.64.0.0/10")


def _validate_endpoint(host: str, port: int, token: str) -> None:
    """Never send file bytes to a LAN or public Internet endpoint."""
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise ValueError("peer endpoint must use Tailnet IPv4") from exc
    if address not in _TAILSCALE_IPV4:
        raise ValueError("peer endpoint must use Tailnet IPv4")
    if not (1 <= port <= 65535):
        raise ValueError("peer endpoint port is invalid")
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise ValueError("peer capability token is invalid")


async def send_peer_transfer(
    config: ExecutorConfig,
    store: ToolSessionStore,
    *,
    session_id: str,
    path: str,
    transfer_id: str,
    host: str,
    port: int,
    token: str,
    tailnet_id: str,
    expected_bytes: int,
    expected_sha256: str,
    chunk_size: int | None = None,
    unbound_temp: bool = False,
) -> dict[str, str | int]:
    """Stream a frozen local source directly to a one-use overlay receiver.

    Only the bounded acknowledgement is returned; credentials stay private to
    the executor RPC and never appear in returned data or exception messages.
    """
    _validate_endpoint(host, port, token)
    if not (0 <= expected_bytes <= _MAX_TRANSFER_BYTES):
        raise ValueError("invalid peer transfer size")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("invalid peer transfer digest")
    if (
        len(transfer_id) > 128
        or re.fullmatch(r"copy_[A-Za-z0-9_-]{22,}", transfer_id) is None
    ):
        raise ValueError("invalid peer transfer identity")

    source_workdir = store.admit_active_session(session_id).workdir

    def verify_source_session() -> None:
        if store.admit_active_session(session_id).workdir != source_workdir:
            raise RuntimeError(
                "source session workdir changed during peer transfer"
            )

    overlay = await asyncio.to_thread(detect_tailscale_overlay)
    if overlay is None or overlay.tailnet_id != tailnet_id:
        raise RuntimeError("trusted overlay is unavailable or changed")

    source_args = {
        "session_id": session_id,
        "path": path,
        "expected_bytes": expected_bytes,
        "expected_sha256": expected_sha256,
        "chunk_size": normalize_chunk_size(chunk_size),
        "_workgate_unbound_temp": unbound_temp,
    }
    handle, source_stat, _, _, size = _open_source(config, store, source_args)
    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), _CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            raise RuntimeError(
                "trusted peer connection is unavailable"
            ) from exc
        try:
            writer.write(token.encode("ascii") + b"\n")
            async with asyncio.timeout(_IO_TIMEOUT_S):
                await writer.drain()
            async for block in _source_content(
                handle,
                source_stat,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                chunk_size=size,
            ):
                verify_source_session()
                writer.write(block)
                async with asyncio.timeout(_IO_TIMEOUT_S):
                    await writer.drain()
            async with asyncio.timeout(_IO_TIMEOUT_S):
                reply = await reader.readline()
            if reply != b"OK\n":
                raise RuntimeError("trusted peer did not confirm the transfer")
            verify_source_session()
            return {
                "transfer_id": transfer_id,
                "bytes": expected_bytes,
                "sha256": expected_sha256,
            }
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    finally:
        handle.close()
