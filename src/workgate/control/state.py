"""Restart-critical durable state owned by the control runtime."""

from collections.abc import Mapping
from threading import RLock
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from ..persistence import StateStore
from ..protocol.credentials import ExecutorCredentialVerifier
from ..protocol.ids import ExecutorId, SessionId, TaskId
from .registry_recovery import load_registry, write_registry

_REGISTRY_VERSION = 1
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class _SyncLockContext(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, *exc_info: object) -> object: ...


class ExecutorTrustRecord(BaseModel):
    """Durable control authority for one paired executor credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executor_id: ExecutorId
    name: str = Field(min_length=1, max_length=80)
    credential_verifier: ExecutorCredentialVerifier
    created_at: Timestamp
    revoked_at: Timestamp | None = None
    draining: bool = False

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
    task_id: TaskId | None = None
    workdir: str | None = Field(default=None, max_length=4096)
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

    def __init__(
        self,
        state_store: StateStore,
        *,
        lock: _SyncLockContext | None = None,
    ) -> None:
        self.state_store = state_store
        self._executors: dict[str, ExecutorTrustRecord] = {}
        self._sessions: dict[str, ControlSessionRecord] = {}
        self._lock = RLock() if lock is None else lock
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

    def set_executor_draining(
        self, executor_id: str, *, draining: bool
    ) -> ExecutorTrustRecord:
        """Persist executor session-admission drain state without changing trust."""
        with self._lock:
            self._require_started()
            current = self._executors.get(executor_id)
            if current is None:
                raise KeyError(executor_id)
            if current.revoked_at is not None:
                raise ValueError(f"executor {executor_id!r} is revoked")
            updated = current.model_copy(update={"draining": draining})
            validated = ExecutorTrustRecord.model_validate(updated.model_dump())
            candidate = {**self._executors, executor_id: validated}
            self._write_executors(candidate)
            self._executors = candidate
            return validated

    def rename_executor(
        self, executor_id: str, *, name: str
    ) -> ExecutorTrustRecord:
        """Persist a friendly-name mutation without changing trust authority."""
        with self._lock:
            self._require_started()
            current = self._executors.get(executor_id)
            if current is None:
                raise KeyError(executor_id)
            updated = current.model_copy(update={"name": name})
            validated = ExecutorTrustRecord.model_validate(updated.model_dump())
            candidate = {**self._executors, executor_id: validated}
            self._write_executors(candidate)
            self._executors = candidate
            return validated

    def put_session(self, record: ControlSessionRecord) -> None:
        """Durably publish one control session lifecycle mutation."""
        with self._lock:
            self._require_started()
            current = self._sessions.get(record.session_id)
            if (
                current is not None
                and current.executor_id != record.executor_id
            ):
                raise ValueError("session executor binding cannot change")
            if current is not None and record.task_id != current.task_id:
                raise ValueError(
                    "session task attachment cannot change through lifecycle updates"
                )
            candidate = {**self._sessions, record.session_id: record}
            self._write_sessions(candidate)
            self._sessions = candidate

    def update_session(
        self, session_id: str, **changes: Any
    ) -> ControlSessionRecord:
        """Patch lifecycle fields while preserving immutable session bindings."""
        allowed = {
            "status",
            "workdir",
            "updated_at",
        }
        unsupported = set(changes) - allowed
        if unsupported:
            raise ValueError(
                "unsupported session lifecycle field(s): "
                + ", ".join(sorted(unsupported))
            )
        with self._lock:
            self._require_started()
            current = self._sessions.get(session_id)
            if current is None:
                raise KeyError(session_id)
            updated = ControlSessionRecord.model_validate(
                current.model_copy(update=changes).model_dump()
            )
            candidate = {**self._sessions, session_id: updated}
            self._write_sessions(candidate)
            self._sessions = candidate
            return updated

    def attach_session_task(
        self, session_id: str, task_id: str
    ) -> ControlSessionRecord:
        """Durably attach one execution session to one task."""
        with self._lock:
            self._require_started()
            current = self._sessions.get(session_id)
            if current is None:
                raise KeyError(session_id)
            if current.task_id is not None and current.task_id != task_id:
                raise ValueError("session task attachment cannot change")
            if current.task_id == task_id:
                return current
            updated = ControlSessionRecord.model_validate(
                current.model_copy(update={"task_id": task_id}).model_dump()
            )
            candidate = {**self._sessions, session_id: updated}
            self._write_sessions(candidate)
            self._sessions = candidate
            return updated

    def detach_task(self, task_id: str) -> None:
        """Clear a deleted task from retained sessions."""
        with self._lock:
            self._require_started()
            changed = False
            candidate = dict(self._sessions)
            for session_id, current in self._sessions.items():
                if str(current.task_id or "") != task_id:
                    continue
                candidate[session_id] = ControlSessionRecord.model_validate(
                    current.model_copy(update={"task_id": None}).model_dump()
                )
                changed = True
            if changed:
                self._write_sessions(candidate)
                self._sessions = candidate

    def remove_session(self, session_id: str) -> None:
        """Durably forget a checkpoint only when no executor side effect exists."""
        with self._lock:
            self._require_started()
            if session_id not in self._sessions:
                return
            candidate = dict(self._sessions)
            candidate.pop(session_id, None)
            self._write_sessions(candidate)
            self._sessions = candidate

    def _require_started(self) -> None:
        if self._closed or not self._started:
            raise RuntimeError("ControlState is not running")

    def _load_executors(self) -> dict[str, ExecutorTrustRecord]:
        path = self.state_store.layout.control_executors_path
        with self.state_store.transaction(path):
            return load_registry(self.state_store, path, self._parse_executors)

    def _parse_executors(self, payload: Any) -> dict[str, ExecutorTrustRecord]:
        path = self.state_store.layout.control_executors_path
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
            return load_registry(self.state_store, path, self._parse_sessions)

    def _parse_sessions(self, payload: Any) -> dict[str, ControlSessionRecord]:
        path = self.state_store.layout.control_sessions_path
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
            write_registry(self.state_store, path, payload)

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
            write_registry(self.state_store, path, payload)
