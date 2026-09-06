"""Restart-critical durable state owned by the control runtime."""

from __future__ import annotations

from collections.abc import Mapping
from threading import RLock
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from ..persistence import StateStore
from ..protocol.credentials import ExecutorCredentialVerifier
from ..protocol.ids import ExecutorId, SessionId

_REGISTRY_VERSION = 1
_MAX_REGISTRY_BYTES = 4 * 1024 * 1024
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ExecutorTrustRecord(BaseModel):
    """Durable control authority for one paired executor credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executor_id: ExecutorId
    name: str = Field(min_length=1, max_length=80)
    credential_verifier: ExecutorCredentialVerifier
    created_at: Timestamp
    revoked_at: Timestamp | None = None

    @model_validator(mode="after")
    def validate_revocation_time(self) -> ExecutorTrustRecord:
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at cannot precede created_at")
        return self


class ControlSessionRecord(BaseModel):
    """Durable control-side binding and lifecycle intent for one session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: SessionId
    executor_id: ExecutorId
    requested_workdir: str = Field(min_length=1, max_length=4096)
    resolved_workdir_display: str | None = Field(default=None, max_length=4096)
    label: str | None = Field(default=None, max_length=256)
    status: Literal["creating", "active", "terminating", "ended"]
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def validate_update_time(self) -> ControlSessionRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class ControlState:
    """Own small write-through registries for restart-critical control facts."""

    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store
        self._executors: dict[str, ExecutorTrustRecord] = {}
        self._sessions: dict[str, ControlSessionRecord] = {}
        self._lock = RLock()
        self._started = False
        self._closed = False

    def start(self) -> None:
        """Load durable control facts transactionally before admitting mutations."""
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    "ControlState cannot be restarted after close"
                )
            if self._started:
                return
            executors = self._load_executors()
            sessions = self._load_sessions()
            self._executors = executors
            self._sessions = sessions
            self._started = True

    def close(self) -> None:
        """Discard process-local projections without deleting durable state."""
        with self._lock:
            if self._closed:
                return
            self._executors.clear()
            self._sessions.clear()
            self._started = False
            self._closed = True

    def snapshot_executors(self) -> Mapping[str, ExecutorTrustRecord]:
        """Return a detached view of the currently loaded trust records."""
        with self._lock:
            return dict(self._executors)

    def snapshot_sessions(self) -> Mapping[str, ControlSessionRecord]:
        """Return a detached view of the currently loaded session records."""
        with self._lock:
            return dict(self._sessions)

    def put_executor(self, record: ExecutorTrustRecord) -> None:
        """Durably publish one executor trust mutation before exposing it in memory."""
        with self._lock:
            self._require_started()
            candidate = {**self._executors, record.executor_id: record}
            self._write_executors(candidate)
            self._executors = candidate

    def revoke_executor(
        self, executor_id: str, *, revoked_at: float
    ) -> ExecutorTrustRecord:
        """Persist executor revocation before exposing the revoked record to callers."""
        with self._lock:
            self._require_started()
            current = self._executors.get(executor_id)
            if current is None:
                raise KeyError(executor_id)
            updated = current.model_copy(update={"revoked_at": revoked_at})
            validated = ExecutorTrustRecord.model_validate(updated.model_dump())
            candidate = {**self._executors, executor_id: validated}
            self._write_executors(candidate)
            self._executors = candidate
            return validated

    def put_session(self, record: ControlSessionRecord) -> None:
        """Durably publish one control session lifecycle mutation."""
        with self._lock:
            self._require_started()
            candidate = {**self._sessions, record.session_id: record}
            self._write_sessions(candidate)
            self._sessions = candidate

    def _require_started(self) -> None:
        if self._closed or not self._started:
            raise RuntimeError("ControlState is not running")

    def _load_executors(self) -> dict[str, ExecutorTrustRecord]:
        path = self.state_store.layout.control_executors_path
        with self.state_store.transaction(path):
            payload = self.state_store.read_json(
                path, max_bytes=_MAX_REGISTRY_BYTES
            )
        rows = self._registry_rows(payload, field="executors", path=path)
        records: dict[str, ExecutorTrustRecord] = {}
        try:
            for row in rows:
                record = ExecutorTrustRecord.model_validate(row)
                if record.executor_id in records:
                    raise ValueError(
                        f"duplicate executor_id: {record.executor_id}"
                    )
                records[record.executor_id] = record
        except (ValidationError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid control executor registry: {path}"
            ) from exc
        return records

    def _load_sessions(self) -> dict[str, ControlSessionRecord]:
        path = self.state_store.layout.control_sessions_path
        with self.state_store.transaction(path):
            payload = self.state_store.read_json(
                path, max_bytes=_MAX_REGISTRY_BYTES
            )
        rows = self._registry_rows(payload, field="sessions", path=path)
        records: dict[str, ControlSessionRecord] = {}
        try:
            for row in rows:
                record = ControlSessionRecord.model_validate(row)
                if record.session_id in records:
                    raise ValueError(
                        f"duplicate session_id: {record.session_id}"
                    )
                records[record.session_id] = record
        except (ValidationError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid control session registry: {path}"
            ) from exc
        return records

    @staticmethod
    def _registry_rows(
        payload: Any | None, *, field: str, path: object
    ) -> list[Any]:
        if payload is None:
            return []
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _REGISTRY_VERSION
        ):
            raise RuntimeError(f"Unsupported control registry format: {path}")
        rows = payload.get(field)
        if not isinstance(rows, list):
            raise RuntimeError(f"Invalid control registry contents: {path}")
        return rows

    def _write_executors(
        self, records: Mapping[str, ExecutorTrustRecord]
    ) -> None:
        path = self.state_store.layout.control_executors_path
        payload = {
            "version": _REGISTRY_VERSION,
            "executors": [
                records[key].model_dump(mode="json") for key in sorted(records)
            ],
        }
        with self.state_store.transaction(path):
            self.state_store.write_json(path, payload)

    def _write_sessions(
        self, records: Mapping[str, ControlSessionRecord]
    ) -> None:
        path = self.state_store.layout.control_sessions_path
        payload = {
            "version": _REGISTRY_VERSION,
            "sessions": [
                records[key].model_dump(mode="json") for key in sorted(records)
            ],
        }
        with self.state_store.transaction(path):
            self.state_store.write_json(path, payload)
