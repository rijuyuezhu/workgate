"""Durable tool-session record models and JSON codecs."""

import json
import re
import secrets
from dataclasses import asdict, dataclass
from typing import Any, cast

_SESSION_ID_RE = re.compile(r"^sess_[A-Za-z0-9_-]{22,}$")
SESSION_ID_MAX_LENGTH = 128


@dataclass(frozen=True)
class AgentSession:
    """One explicit agent workspace session."""

    session_id: str
    """Opaque shared control/executor session identifier."""
    workdir: str
    """Current workspace root resolved by this executor."""
    created_at: float
    """Unix timestamp when the session was created."""
    updated_at: float
    """Unix timestamp of the latest durable session update."""
    label: str | None = None
    """Optional human-readable session label."""
    termination_requested_at: float | None = None
    """Unix timestamp of an irreversible termination request, when present."""
    persistent_shell_ids: tuple[str, ...] = ()
    """Persistent shells currently owned by this session."""


@dataclass(frozen=True)
class SnapshotRecord:
    """One displayed file snapshot recorded for stale-edit checks."""

    session_id: str
    """Owning agent session identifier."""
    snapshot_id: str
    """Opaque identifier returned with the displayed snapshot."""
    path: str
    """Workspace-relative or absolute path represented by the snapshot."""
    file_sha256: str
    """SHA-256 digest of the complete file at display time."""
    total_lines: int
    """Total line count of the complete file at display time."""
    seen_ranges: tuple[tuple[int, int], ...]
    """Inclusive line ranges that were actually shown to the caller."""
    created_at: float
    """Unix timestamp when the snapshot was recorded."""
    sequence: int = 0
    """Monotonic per-session sequence used for retention ordering."""


def valid_session_id(value: Any) -> str | None:
    """Return one final shared session id, or None for invalid input."""
    if not isinstance(value, str):
        return None
    if len(value) <= SESSION_ID_MAX_LENGTH and _SESSION_ID_RE.fullmatch(value):
        return value
    return None


def new_snapshot_id() -> str:
    """Return an opaque snapshot id for one displayed file view."""
    return secrets.token_hex(6)


def required_float(payload: dict[str, Any], field: str) -> float:
    """Read one required finite numeric metadata field."""
    candidate = payload.get(field)
    if isinstance(candidate, bool) or not isinstance(candidate, int | float):
        raise ValueError(f"metadata contains an invalid {field}")
    return float(candidate)


def required_int(payload: dict[str, Any], field: str) -> int:
    """Read one required integer metadata field."""
    candidate = payload.get(field)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise ValueError(f"metadata contains an invalid {field}")
    return candidate


def session_from_payload(value: Any) -> AgentSession:
    """Decode and validate one durable session metadata payload."""
    if not isinstance(value, dict):
        raise ValueError("session metadata must be a JSON object")
    payload = cast(dict[str, Any], value)
    session_id = valid_session_id(payload.get("session_id"))
    if session_id is None:
        raise ValueError("session metadata contains an invalid session_id")
    workdir = str(payload.get("workdir") or "")
    if not workdir:
        raise ValueError("session metadata contains an empty workdir")
    shell_ids = payload.get("persistent_shell_ids", [])
    if not isinstance(shell_ids, list):
        raise ValueError(
            "session metadata contains invalid persistent_shell_ids"
        )
    normalized_shell_ids = tuple(
        dict.fromkeys(str(shell_id) for shell_id in shell_ids if shell_id)
    )
    return AgentSession(
        session_id=session_id,
        workdir=workdir,
        created_at=required_float(payload, "created_at"),
        updated_at=required_float(payload, "updated_at"),
        label=None if payload.get("label") is None else str(payload["label"]),
        termination_requested_at=(
            None
            if payload.get("termination_requested_at") is None
            else required_float(payload, "termination_requested_at")
        ),
        persistent_shell_ids=normalized_shell_ids,
    )


def session_to_payload(session: AgentSession) -> dict[str, Any]:
    """Encode one canonical session record using the durable reader invariants."""
    payload = asdict(session)
    payload["persistent_shell_ids"] = list(session.persistent_shell_ids)
    decoded = session_from_payload(payload)
    if decoded != session:
        raise ValueError("session record is not canonical for durable storage")
    return payload


def snapshot_from_payload(
    value: Any, *, fallback_sequence: int = 0
) -> SnapshotRecord:
    """Decode and validate one durable snapshot metadata payload."""
    if not isinstance(value, dict):
        raise ValueError("snapshot metadata must be a JSON object")
    payload = cast(dict[str, Any], value)
    ranges = payload.get("seen_ranges")
    if not isinstance(ranges, list):
        raise ValueError("snapshot metadata contains invalid seen_ranges")
    normalized_ranges: list[tuple[int, int]] = []
    for item in ranges:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise ValueError("snapshot metadata contains an invalid range")
        normalized_ranges.append((int(item[0]), int(item[1])))
    sequence = (
        fallback_sequence
        if payload.get("sequence") is None
        else required_int(payload, "sequence")
    )
    if sequence < 0:
        raise ValueError("snapshot metadata contains invalid sequence")
    return SnapshotRecord(
        session_id=str(payload.get("session_id") or ""),
        snapshot_id=str(payload.get("snapshot_id") or ""),
        path=str(payload.get("path") or ""),
        file_sha256=str(payload.get("file_sha256") or ""),
        total_lines=required_int(payload, "total_lines"),
        seen_ranges=tuple(normalized_ranges),
        created_at=required_float(payload, "created_at"),
        sequence=sequence,
    )


def encoded_snapshot_payload_bytes(snapshots: list[SnapshotRecord]) -> int:
    """Return exact bytes produced by StateStore.write_json for snapshots."""
    encoded = json.dumps(
        {"snapshots": [asdict(snapshot) for snapshot in snapshots]},
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
    return len((encoded + "\n").encode("utf-8"))
