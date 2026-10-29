"""Executor runtime ownership; legacy frozen states remain readable."""

import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..persistence import StateStore
from ..protocol.executor import ExecutorRuntimeOwnership

_RUNTIME_STATE_MAX_BYTES = 16 * 1024


class ExecutorRuntimeUpdateStatus(StrEnum):
    """Executor-local runtime update lifecycle."""

    IDLE = "idle"
    REQUIRED = "required"
    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class ExecutorRuntimeState(BaseModel):
    """Small durable statement of runtime ownership and update health."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ownership: ExecutorRuntimeOwnership
    executable: str | None = Field(default=None, min_length=1, max_length=4096)
    update_status: ExecutorRuntimeUpdateStatus = (
        ExecutorRuntimeUpdateStatus.IDLE
    )
    update_target_version: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    update_detail: str | None = Field(
        default=None, min_length=1, max_length=1000
    )


class ExecutorRuntimeStateStore:
    """Persist executor runtime ownership separately from pairing identity."""

    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store

    def load(self) -> ExecutorRuntimeState | None:
        path = self.state_store.layout.executor_runtime_path
        with self.state_store.transaction(path):
            payload = self.state_store.read_json(
                path, max_bytes=_RUNTIME_STATE_MAX_BYTES
            )
        if payload is None:
            return None
        try:
            return ExecutorRuntimeState.model_validate(payload)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid executor runtime state: {path}"
            ) from exc

    def save(self, state: ExecutorRuntimeState) -> ExecutorRuntimeState:
        path = self.state_store.layout.executor_runtime_path
        with self.state_store.transaction(path):
            self.state_store.write_json(path, state.model_dump(mode="json"))
        return state

    def install_state(
        self,
        ownership: ExecutorRuntimeOwnership | None = None,
    ) -> ExecutorRuntimeState:
        selected = ownership or detect_runtime_ownership()
        executable = None
        if selected is ExecutorRuntimeOwnership.CONTROL_MANAGED:
            executable = _normalized_executable(current_frozen_executable())
            if executable is None:
                raise RuntimeError(
                    "control-managed runtime ownership requires a self-contained executable"
                )
        return ExecutorRuntimeState(ownership=selected, executable=executable)

    def record_install(
        self,
        *,
        ownership: ExecutorRuntimeOwnership | None = None,
    ) -> ExecutorRuntimeState:
        """Record the runtime owner of a successfully installed service."""
        return self.save(self.install_state(ownership))

    def service_runtime_state(
        self,
        ownership: ExecutorRuntimeOwnership | None = None,
    ) -> ExecutorRuntimeState:
        """Return runtime state for the installed service process."""
        current = self.load()
        if ownership is not None:
            expected = self.install_state(ownership)
            if (
                current is None
                or current.ownership is not expected.ownership
                or current.executable != expected.executable
            ):
                return self.save(expected)
            return current
        if current is None:
            return self.record_install()
        if current.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED:
            executable = _normalized_executable(current_frozen_executable())
            if executable != current.executable:
                return self.record_install()
        return current

    def _require_state(self) -> ExecutorRuntimeState:
        current = self.load()
        if current is None:
            raise RuntimeError(
                "executor service runtime ownership is not recorded"
            )
        return current

    def set_update(
        self,
        status: ExecutorRuntimeUpdateStatus,
        *,
        target_version: str | None = None,
        detail: str | None = None,
    ) -> ExecutorRuntimeState:
        """Persist one local update state transition."""
        if status is ExecutorRuntimeUpdateStatus.IDLE:
            target_version = None
            detail = None
        elif target_version is None:
            raise ValueError(
                "non-idle runtime update state requires a target version"
            )
        current = self._require_state()
        next_state = current.model_copy(
            update={
                "update_status": status,
                "update_target_version": target_version,
                "update_detail": None if detail is None else detail[:1000],
            }
        )
        return current if next_state == current else self.save(next_state)


def _normalized_executable(path: Path | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path).expanduser().absolute()
    return str(candidate)


def detect_runtime_ownership() -> ExecutorRuntimeOwnership:
    """Classify the current runtime conservatively when no explicit owner exists."""
    if bool(getattr(sys, "frozen", False)):
        return ExecutorRuntimeOwnership.SELF_CONTAINED
    try:
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "pyproject.toml").is_file() and (
                parent / "src" / "workgate"
            ).is_dir():
                return ExecutorRuntimeOwnership.SOURCE
    except OSError:
        return ExecutorRuntimeOwnership.UNKNOWN
    return ExecutorRuntimeOwnership.EXTERNAL_PACKAGE


def current_frozen_executable() -> Path | None:
    """Return this process executable only for self-contained frozen runtimes."""
    if not bool(getattr(sys, "frozen", False)):
        return None
    return Path(sys.executable).expanduser().absolute()
