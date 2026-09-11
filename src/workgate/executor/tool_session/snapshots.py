"""Durable grounding-snapshot repository below the session lifecycle authority."""

from collections.abc import Callable
from dataclasses import asdict

from ...config.settings import Settings
from ...persistence import StateStore
from .records import (
    SnapshotRecord,
    encoded_snapshot_payload_bytes,
    snapshot_from_payload,
)

SESSION_SNAPSHOTS_MAX_BYTES = 16_000_000


class SnapshotRepository:
    """Load, retain, and persist per-session grounding snapshots.

    Callers retain ownership of the session transaction and admission check. This
    repository owns only snapshot format, cache, retention, and filesystem IO.
    """

    def __init__(
        self,
        state_store: StateStore,
        settings_provider: Callable[[], Settings],
    ) -> None:
        self._state_store = state_store
        self._settings_provider = settings_provider
        self._snapshots: dict[tuple[str, str], SnapshotRecord] = {}

    def clear_cache(self) -> None:
        """Drop all process-local snapshot state without touching durable files."""
        self._snapshots.clear()

    def forget_session(self, session_id: str) -> None:
        """Drop one session from the process-local snapshot cache."""
        self._snapshots = {
            key: value
            for key, value in self._snapshots.items()
            if key[0] != session_id
        }

    def remove_session(self, session_id: str) -> None:
        """Remove one session's durable snapshot index and cached records."""
        self.forget_session(session_id)
        self._state_store.remove(
            self._state_store.layout.session_snapshots_path(session_id)
        )

    def record(
        self,
        *,
        session_id: str,
        snapshot_id: str,
        path: str,
        file_sha256: str,
        total_lines: int,
        seen_ranges: tuple[tuple[int, int], ...],
        created_at: float,
    ) -> SnapshotRecord:
        """Persist one new snapshot under an already-admitted session transaction."""
        self._load(session_id)
        next_sequence = 1 + max(
            (
                snapshot.sequence
                for (owner, _), snapshot in self._snapshots.items()
                if owner == session_id
            ),
            default=0,
        )
        record = SnapshotRecord(
            session_id=session_id,
            snapshot_id=snapshot_id,
            path=path,
            file_sha256=file_sha256,
            total_lines=total_lines,
            seen_ranges=seen_ranges,
            created_at=created_at,
            sequence=next_sequence,
        )
        maximum_bytes = int(
            self._settings_provider().max_session_snapshot_bytes
        )
        if encoded_snapshot_payload_bytes([record]) > maximum_bytes:
            raise ValueError(
                "snapshot metadata exceeds max_session_snapshot_bytes"
            )
        self._snapshots[(session_id, snapshot_id)] = record
        self._prune(session_id)
        self._write(session_id)
        return record

    def get(self, session_id: str, snapshot_id: str) -> SnapshotRecord | None:
        """Load and return one durable snapshot, if retained."""
        self._load(session_id)
        return self._snapshots.get((session_id, snapshot_id))

    def _load(self, session_id: str) -> None:
        self.forget_session(session_id)
        payload = self._state_store.read_json(
            self._state_store.layout.session_snapshots_path(session_id),
            max_bytes=SESSION_SNAPSHOTS_MAX_BYTES,
        )
        if payload is None:
            return
        if not isinstance(payload, dict) or not isinstance(
            payload.get("snapshots"), list
        ):
            raise ValueError("session snapshots must contain a snapshots array")
        for sequence, value in enumerate(payload["snapshots"], start=1):
            snapshot = snapshot_from_payload(value, fallback_sequence=sequence)
            if snapshot.session_id != session_id:
                raise ValueError(
                    "snapshot owner does not match its session directory"
                )
            self._snapshots[(session_id, snapshot.snapshot_id)] = snapshot

    def _write(self, session_id: str) -> None:
        snapshots = [
            asdict(snapshot)
            for (owner, _), snapshot in self._snapshots.items()
            if owner == session_id
        ]
        path = self._state_store.layout.session_snapshots_path(session_id)
        if not snapshots:
            self._state_store.remove(path)
            return
        self._state_store.write_json(path, {"snapshots": snapshots})

    def _prune(self, session_id: str) -> None:
        """Keep newest snapshots within configured count and byte limits."""
        settings = self._settings_provider()
        snapshots = sorted(
            (
                snapshot
                for (owner, _), snapshot in self._snapshots.items()
                if owner == session_id
            ),
            key=lambda snapshot: (snapshot.created_at, snapshot.sequence),
            reverse=True,
        )[: int(settings.max_session_snapshots)]
        maximum_bytes = int(settings.max_session_snapshot_bytes)
        low = 0
        high = len(snapshots)
        while low < high:
            middle = (low + high + 1) // 2
            if (
                encoded_snapshot_payload_bytes(snapshots[:middle])
                <= maximum_bytes
            ):
                low = middle
            else:
                high = middle - 1
        kept = snapshots[:low]
        self.forget_session(session_id)
        self._snapshots.update(
            {(session_id, snapshot.snapshot_id): snapshot for snapshot in kept}
        )
