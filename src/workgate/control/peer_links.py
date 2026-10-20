"""Control-owned, on-demand peer path evidence. Never performs a transfer."""

import asyncio
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .executor_transport import ExecutorTransport
from .state import ControlState

_FRESH_SECONDS = 300
_PROBE_TIMEOUT_SECONDS = 17
_RETEST_SECONDS = 10
_MAX_CACHE_ENTRIES = 128
_MAX_SIMULTANEOUS = 2

LinkOutcome = Literal[
    "untested", "peer_succeeded", "unreachable", "ineligible", "stale"
]


class LinkProbeBusy(ValueError):
    """An explicit probe exceeds the per-pair or process-wide limits."""


@dataclass(frozen=True)
class LinkResult:
    source_executor_id: str
    destination_executor_id: str
    status: LinkOutcome
    tested_at: str | None = None
    reason: str | None = None
    previous_status: LinkOutcome | None = None
    evidence: str = "none"
    fallback: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "source_executor_id": self.source_executor_id,
            "destination_executor_id": self.destination_executor_id,
            "status": self.status,
            "tested_at": self.tested_at,
            "reason": self.reason,
            "previous_status": self.previous_status,
            "evidence": self.evidence,
            "fallback": self.fallback,
            "directional": True,
        }


class ControlPeerLinkService:
    """Small process-local evidence cache, never a background topology scanner."""

    def __init__(
        self,
        control_state: ControlState,
        transport: ExecutorTransport,
    ) -> None:
        self._state = control_state
        self._transport = transport
        self._records: dict[tuple[str, str], tuple[float, LinkResult]] = {}
        self._attempted: dict[tuple[str, str], float] = {}
        self._active: set[tuple[str, str]] = set()
        self._closed = False

    def _pair(self, source_id: str, destination_id: str) -> tuple[str, str]:
        if (
            not source_id
            or not destination_id
            or source_id == destination_id
            or len(source_id.encode("utf-8")) > 128
            or len(destination_id.encode("utf-8")) > 128
        ):
            raise ValueError("Two distinct Executor IDs required")
        trusted = self._state.snapshot_executors()
        if any(
            executor_id not in trusted
            or trusted[executor_id].revoked_at is not None
            for executor_id in (source_id, destination_id)
        ):
            raise LookupError("Executor not found")
        return source_id, destination_id

    async def read(self, source_id: str, destination_id: str) -> dict[str, Any]:
        key = self._pair(source_id, destination_id)
        entry = self._records.get(key)
        if entry is None:
            return LinkResult(*key, status="untested").payload()
        received, row = entry
        fresh = time.monotonic() - received < _FRESH_SECONDS
        online = await asyncio.gather(
            self._transport.is_online(source_id),
            self._transport.is_online(destination_id),
        )
        if fresh and all(online):
            return row.payload()
        return LinkResult(
            *key,
            status="stale",
            tested_at=row.tested_at,
            reason="measurement_expired" if not fresh else "executor_offline",
            previous_status=row.status,
            evidence="historical_" + row.evidence,
            fallback=row.fallback,
        ).payload()

    async def _probe_real(
        self, source_id: str, destination_id: str
    ) -> tuple[LinkOutcome, str | None]:
        if not all(
            await asyncio.gather(
                self._transport.is_online(source_id),
                self._transport.is_online(destination_id),
            )
        ):
            return "unreachable", "executor_offline"
        src_hello, dst_hello = await asyncio.gather(
            self._transport.inventory(source_id),
            self._transport.inventory(destination_id),
        )
        source_overlay = src_hello.overlay if src_hello is not None else None
        destination_overlay = (
            dst_hello.overlay if dst_hello is not None else None
        )
        if source_overlay is None or destination_overlay is None:
            return "ineligible", "overlay_unavailable"
        if (
            source_overlay.provider != "tailscale"
            or destination_overlay.provider != "tailscale"
            or source_overlay.tailnet_id != destination_overlay.tailnet_id
        ):
            return "ineligible", "tailnet_mismatch"
        tailnet = source_overlay.tailnet_id
        prepared: dict[str, Any] | None = None
        try:
            receiver = await self._transport.call(
                destination_id,
                "peer.probe_prepare",
                {"tailnet_id": tailnet},
                timeout_s=6,
            )
            if not receiver.ok or not isinstance(receiver.result, dict):
                return "unreachable", "receiver_unavailable"
            prepared = receiver.result
            host = prepared.get("host")
            port = prepared.get("port")
            token = prepared.get("token")
            probe_id = prepared.get("probe_id")
            if (
                host != destination_overlay.ipv4
                or type(port) is not int
                or not 1 <= port <= 65535
                or not isinstance(token, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None
                or not isinstance(probe_id, str)
                or not probe_id.startswith("probe_")
                or len(probe_id) > 80
            ):
                return "unreachable", "receiver_invalid"
            sender = await self._transport.call(
                source_id,
                "peer.probe_connect",
                {
                    "host": host,
                    "port": port,
                    "token": token,
                    "tailnet_id": tailnet,
                },
                timeout_s=7,
            )
            if not sender.ok or sender.result != {"connected": True}:
                return "unreachable", "peer_handshake_failed"
            return "peer_succeeded", None
        finally:
            # The receiver also expires locally if this RPC cannot be delivered.
            if prepared is not None and isinstance(
                prepared.get("probe_id"), str
            ):
                # Ephemeral receiver expires independently if cleanup RPC fails.
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(
                        asyncio.shield(
                            self._transport.call(
                                destination_id,
                                "peer.probe_cancel",
                                {"probe_id": prepared["probe_id"]},
                                timeout_s=3,
                            )
                        ),
                        timeout=4,
                    )

    async def probe(
        self, source_id: str, destination_id: str
    ) -> dict[str, Any]:
        key = self._pair(source_id, destination_id)
        now = time.monotonic()
        if (
            self._closed
            or key in self._active
            or len(self._active) >= _MAX_SIMULTANEOUS
        ):
            raise LinkProbeBusy("Peer link probe is busy")
        last = self._attempted.get(key)
        if last is not None and now - last < _RETEST_SECONDS:
            raise LinkProbeBusy("Peer link probe was checked too recently")
        self._active.add(key)
        self._attempted[key] = now
        if len(self._attempted) > _MAX_CACHE_ENTRIES:
            oldest = min(self._attempted, key=self._attempted.__getitem__)
            if oldest != key:
                self._attempted.pop(oldest)
        try:
            try:
                async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
                    state, reason = await self._probe_real(*key)
            except TimeoutError:
                state, reason = "unreachable", "probe_timeout"
            except RuntimeError, ValueError, OSError, ConnectionError:
                state, reason = "unreachable", "probe_unavailable"
            row = LinkResult(
                *key,
                status=state,
                tested_at=datetime.now(UTC).isoformat(),
                reason=reason,
                evidence="peer_handshake"
                if state == "peer_succeeded"
                else (
                    "overlay_only" if state == "ineligible" else "probe_failed"
                ),
                fallback="control_relay_not_tested"
                if state != "peer_succeeded"
                else None,
            )
            self._records[key] = (time.monotonic(), row)
            if len(self._records) > _MAX_CACHE_ENTRIES:
                oldest = min(
                    self._records, key=lambda item: self._records[item][0]
                )
                self._records.pop(oldest)
            return row.payload()
        finally:
            self._active.discard(key)

    async def aclose(self) -> None:
        self._closed = True
        self._records.clear()
        self._attempted.clear()
