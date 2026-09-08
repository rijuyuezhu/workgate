"""Durable tool-session record models and JSON codecs."""

import json
import secrets
import string
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

SESSION_ID_ALPHABET = string.ascii_letters + string.digits
SESSION_ID_LENGTH = 8
type SessionTarget = Literal["local", "remote"]


@dataclass(frozen=True)
class AgentSession:
    """One explicit agent workspace session."""

    session_id: str
    """Opaque controller-issued session identifier."""
    target: SessionTarget
    """Execution target class for the session."""
    workdir: str
    """Current workspace root resolved on the selected target."""
    machine: str | None
    """Remote machine name, or None for local sessions."""
    worker_session_id: str | None
    """Worker-side session identifier for remote sessions."""
    created_at: float
    """Unix timestamp when the session was created."""
    updated_at: float
    """Unix timestamp of the latest durable session update."""
    expires_at: float | None = None
    """Optional Unix expiry timestamp for the session."""
    label: str | None = None
    """Optional human-readable session label."""
    termination_requested_at: float | None = None
    """Unix timestamp of an irreversible termination request, when present."""
    persistent_shell_ids: tuple[str, ...] = ()
    """Persistent shells currently owned by this local session."""


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


def generate_session_id() -> str:
    """Return one legacy local-session id during the PR6 routing migration."""
    return "".join(
        secrets.choice(SESSION_ID_ALPHABET) for _ in range(SESSION_ID_LENGTH)
    )


def valid_session_id(value: Any) -> str | None:
    """Return one normalized agent session id, or None for invalid input."""
    if not isinstance(value, str):
        return None
    if len(value) == SESSION_ID_LENGTH and all(
        character in SESSION_ID_ALPHABET for character in value
    ):
        # Read pre-PR6 durable records while all new allocations use the final
        # shared ``sess_...`` namespace.
        return value
    if (
        value.startswith("sess_")
        and len(value) >= 27
        and all(
            character in (SESSION_ID_ALPHABET + "_-") for character in value[5:]
        )
    ):
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
    target_value = str(payload.get("target") or "")
    if target_value not in {"local", "remote"}:
        raise ValueError("session metadata contains an invalid target")
    target = cast(SessionTarget, target_value)
    workdir = str(payload.get("workdir") or "")
    if not workdir:
        raise ValueError("session metadata contains an empty workdir")
    machine = payload.get("machine")
    worker_session_id = payload.get("worker_session_id")
    if target == "local":
        machine = None
        worker_session_id = None
    elif not machine or not worker_session_id:
        raise ValueError(
            "remote session metadata is missing its worker binding"
        )
    shell_ids = payload.get("persistent_shell_ids", [])
    if not isinstance(shell_ids, list):
        raise ValueError(
            "session metadata contains invalid persistent_shell_ids"
        )
    normalized_shell_ids = tuple(
        dict.fromkeys(str(shell_id) for shell_id in shell_ids if shell_id)
    )
    if target == "remote":
        normalized_shell_ids = ()
    return AgentSession(
        session_id=session_id,
        target=target,
        workdir=workdir,
        machine=None if machine is None else str(machine),
        worker_session_id=(
            None if worker_session_id is None else str(worker_session_id)
        ),
        created_at=required_float(payload, "created_at"),
        updated_at=required_float(payload, "updated_at"),
        expires_at=(
            None
            if payload.get("expires_at") is None
            else required_float(payload, "expires_at")
        ),
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
