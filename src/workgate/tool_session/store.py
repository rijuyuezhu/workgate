"""Durable state for explicit agent/workspace sessions and grounding snapshots."""

import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config.settings import Settings, get_settings
from ..ops.utils.path import resolve_path
from ..persistence import StateStore, get_state_store
from .lifecycle import try_session_lifecycle_lease
from .records import (
    SESSION_ID_ALPHABET as _SESSION_ID_ALPHABET,
)
from .records import (
    SESSION_ID_LENGTH as _SESSION_ID_LENGTH,
)
from .records import (
    AgentSession,
    SessionTarget,
    SnapshotRecord,
    encoded_snapshot_payload_bytes,
    new_snapshot_id,
    session_from_payload,
    snapshot_from_payload,
    valid_session_id,
)
from .records import (
    generate_session_id as _generate_session_id,
)
from .repository import SessionRepository
from .resources import (
    bind_shell,
    ensure_shell_unowned,
    normalize_shell_id,
    release_shell,
    replace_shells,
    require_local_session,
    reserve_shell,
    retain_live_shells,
)
from .resources import (
    persistent_shell_ids as collect_persistent_shell_ids,
)
from .retention import (
    ACTIVE_JOB_STATUSES as _ACTIVE_JOB_STATUSES,
)
from .retention import (
    JOB_STORE_READ_MAX_BYTES as _JOB_STORE_READ_MAX_BYTES,
)
from .retention import (
    JobProtectionReader,
    expired_prune_eligible,
    overflow_prune_eligible,
    session_expired_by_policy,
    session_has_active_jobs,
)
from .snapshots import (
    SESSION_SNAPSHOTS_MAX_BYTES as _SESSION_SNAPSHOTS_MAX_BYTES,
)
from .snapshots import SnapshotRepository

SESSION_ID_ALPHABET = _SESSION_ID_ALPHABET
SESSION_ID_LENGTH = _SESSION_ID_LENGTH
SESSION_METADATA_MAX_BYTES = 256_000
SESSION_SNAPSHOTS_MAX_BYTES = _SESSION_SNAPSHOTS_MAX_BYTES
SESSION_ACTIVE_WINDOW_S = 5 * 60 * 60
JOB_STORE_READ_MAX_BYTES = _JOB_STORE_READ_MAX_BYTES
ACTIVE_JOB_STATUSES = _ACTIVE_JOB_STATUSES
SESSION_TERMINATION_PROMPT = (
    "This session was marked for immediate termination by the human control "
    "plane. Stop immediately. Do not perform any further work or call any more "
    "tools for this session. Tell the user that execution was terminated by "
    "the human operator."
)


class UnknownAgentSessionError(ValueError):
    """Raised when a tool call references a missing agent session."""


class ExpiredAgentSessionError(UnknownAgentSessionError):
    """Raised when a durable agent session has passed its retention boundary."""


class SessionTerminationRequestedError(ValueError):
    """Raised when a model invokes a tool for a terminated session."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session {session_id}: {SESSION_TERMINATION_PROMPT}")


class ToolSessionStore:
    """Persist explicit agent sessions and their grounding snapshots."""

    def __init__(
        self,
        state_store: StateStore | None = None,
        settings_provider: Callable[[], Settings] = get_settings,
    ) -> None:
        self._lock = threading.RLock()
        self._state_store = state_store or get_state_store()
        self._settings_provider = settings_provider
        self._session_repository = SessionRepository(
            self._state_store,
            metadata_max_bytes=SESSION_METADATA_MAX_BYTES,
        )
        self._job_protection_reader = JobProtectionReader(
            self._state_store,
            status_max_bytes=SESSION_METADATA_MAX_BYTES,
        )
        self._snapshot_repository = SnapshotRepository(
            self._state_store, self._settings_provider
        )

    def _settings(self) -> Settings:
        """Return the settings dependency supplied at composition time."""
        return self._settings_provider()

    _session_from_payload = staticmethod(session_from_payload)
    _snapshot_from_payload = staticmethod(snapshot_from_payload)
    _encoded_snapshot_payload_bytes = staticmethod(
        encoded_snapshot_payload_bytes
    )

    def _reset_for_current_root_locked(self) -> None:
        if self._session_repository.reset_for_current_root():
            self._snapshot_repository.clear_cache()

    def _metadata_path(self, session_id: str) -> Path:
        return self._session_repository.metadata_path(session_id)

    def _transaction_path(self, session_id: str) -> Path:
        return self._session_repository.transaction_path(session_id)

    def _remove_session_state_locked(self, session_id: str) -> None:
        """Remove one session directory and all process-local cached state."""
        self._session_repository.remove(session_id)
        self._snapshot_repository.forget_session(session_id)

    def _active_job_session_ids_locked(self) -> set[str] | None:
        """Load sessions protected by active durable jobs, or None if uncertain."""
        return self._job_protection_reader.active_session_ids()

    _session_has_active_jobs = staticmethod(session_has_active_jobs)
    _session_expired_by_policy = staticmethod(session_expired_by_policy)

    def _session_has_active_jobs_locked(self, session_id: str) -> bool:
        """Conservatively protect one session participating in durable jobs."""
        return session_has_active_jobs(
            session_id, self._active_job_session_ids_locked()
        )

    def _session_expired(self, session: AgentSession, now: float) -> bool:
        """Return whether explicit expiry or idle retention invalidates a session."""
        retention_s = int(self._settings().agent_session_retention_s)
        return (
            session_expired_by_policy(session, now, retention_s)
            and not session.persistent_shell_ids
            and not self._session_has_active_jobs_locked(session.session_id)
        )

    def _read_all_sessions_locked(self) -> dict[str, AgentSession]:
        """Load valid durable session metadata without applying retention."""
        return self._session_repository.read_all()

    def _expired_prune_eligible_locked(
        self,
        session: AgentSession,
        now: float,
        retention_s: int,
        active_job_session_ids: set[str] | None,
    ) -> bool:
        """Return whether a freshly loaded session remains expiry-eligible."""
        if session.session_id.startswith("sess_"):
            return False
        return expired_prune_eligible(
            session, now, retention_s, active_job_session_ids
        )

    def _overflow_prune_eligible_locked(
        self,
        session: AgentSession,
        now: float,
        active_job_session_ids: set[str] | None,
    ) -> bool:
        """Return whether a freshly loaded session remains overflow-eligible."""
        if session.session_id.startswith("sess_"):
            return False
        return overflow_prune_eligible(
            session,
            now,
            active_job_session_ids,
            active_window_s=SESSION_ACTIVE_WINDOW_S,
        )

    def _prune_sessions_locked(
        self, now: float, *, reserve_slots: int = 0
    ) -> list[AgentSession]:
        """Remove expired sessions and inactive overflow beyond the configured cap."""
        sessions = self._read_all_sessions_locked()
        active_job_session_ids = self._active_job_session_ids_locked()
        retention_s = int(self._settings().agent_session_retention_s)
        expired = [
            session
            for session in sessions.values()
            if self._expired_prune_eligible_locked(
                session, now, retention_s, active_job_session_ids
            )
        ]
        for session in expired:
            with try_session_lifecycle_lease(
                session.session_id
            ) as lifecycle_available:
                if not lifecycle_available:
                    continue
                with (
                    self._state_store.transaction(
                        self._transaction_path(session.session_id)
                    ),
                    self._state_store.transaction(
                        self._state_store.layout.jobs_lock_path
                    ),
                ):
                    current = self._load_session_locked(session.session_id)
                    if current is None:
                        sessions.pop(session.session_id, None)
                        continue
                    current_jobs = self._active_job_session_ids_locked()
                    if not self._expired_prune_eligible_locked(
                        current, now, retention_s, current_jobs
                    ):
                        sessions[session.session_id] = current
                        continue
                    self._remove_session_state_locked(session.session_id)
                    sessions.pop(session.session_id, None)

        sessions = self._read_all_sessions_locked()
        maximum = int(self._settings().max_agent_sessions)
        target = max(0, maximum - max(0, reserve_slots))
        overflow = max(0, len(sessions) - target)
        if overflow:
            inactive = sorted(
                (
                    session
                    for session in sessions.values()
                    if session.target == "local"
                    and not session.session_id.startswith("sess_")
                    and session.updated_at < now - SESSION_ACTIVE_WINDOW_S
                    and not session.persistent_shell_ids
                    and not self._session_has_active_jobs(
                        session.session_id, active_job_session_ids
                    )
                ),
                key=lambda session: (session.updated_at, session.created_at),
            )
            for session in inactive:
                with try_session_lifecycle_lease(
                    session.session_id
                ) as lifecycle_available:
                    if not lifecycle_available:
                        continue
                    with (
                        self._state_store.transaction(
                            self._transaction_path(session.session_id)
                        ),
                        self._state_store.transaction(
                            self._state_store.layout.jobs_lock_path
                        ),
                    ):
                        current_sessions = self._read_all_sessions_locked()
                        if len(current_sessions) <= target:
                            sessions = current_sessions
                            break
                        current = current_sessions.get(session.session_id)
                        if current is None:
                            sessions = current_sessions
                            continue
                        current_jobs = self._active_job_session_ids_locked()
                        if not self._overflow_prune_eligible_locked(
                            current, now, current_jobs
                        ):
                            sessions = current_sessions
                            continue
                        self._remove_session_state_locked(session.session_id)
                        current_sessions.pop(session.session_id, None)
                        sessions = current_sessions

        self._session_repository.replace_cache(sessions)
        return sorted(
            sessions.values(),
            key=lambda session: (session.updated_at, session.created_at),
            reverse=True,
        )

    def _load_session_locked(self, session_id: str) -> AgentSession | None:
        return self._session_repository.load(session_id)

    def _require_session_locked(
        self, session_id: str, *, allow_expired: bool = False
    ) -> AgentSession:
        session = self._load_session_locked(session_id)
        if session is None:
            raise UnknownAgentSessionError(
                f"unknown session_id {session_id!r}; call session_start first"
            )
        if not allow_expired and self._session_expired(session, time.time()):
            if session.target == "local":
                with try_session_lifecycle_lease(
                    session_id
                ) as lifecycle_available:
                    if lifecycle_available:
                        self._remove_session_state_locked(session_id)
            raise ExpiredAgentSessionError(
                f"expired session_id {session_id!r}; "
                + (
                    "call session_end to release its remote worker binding"
                    if session.target == "remote"
                    else "call session_start again"
                )
            )
        return session

    def _write_session_locked(self, session: AgentSession) -> None:
        self._session_repository.write(session)

    def create_session(
        self,
        *,
        session_id: str | None = None,
        target: SessionTarget = "local",
        workdir: str | Path = ".",
        machine: str | None = None,
        worker_session_id: str | None = None,
        label: str | None = None,
        expires_at: float | None = None,
    ) -> AgentSession:
        """Create and durably store one explicit agent workspace session."""
        if (
            session_id is not None
            and valid_session_id(session_id) != session_id
        ):
            raise ValueError("session_id is invalid")
        if target == "local":
            resolved_workdir = resolve_path(workdir, must_exist=True)
            if not resolved_workdir.is_dir():
                raise NotADirectoryError(str(resolved_workdir))
            display_workdir = str(resolved_workdir)
            normalized_machine = None
            normalized_worker_session_id = None
        elif target == "remote":
            if not machine:
                raise ValueError("machine is required for remote sessions")
            if not worker_session_id:
                raise ValueError(
                    "worker_session_id is required for remote sessions"
                )
            display_workdir = str(workdir)
            if not display_workdir:
                raise ValueError("workdir is required for remote sessions")
            normalized_machine = machine
            normalized_worker_session_id = worker_session_id
        else:
            raise ValueError(f"unsupported session target: {target!r}")

        with self._lock:
            self._reset_for_current_root_locked()
            registry = self._state_store.layout.sessions_dir / ".registry"
            with self._state_store.transaction(registry):
                now = time.time()
                # Control-managed final sessions must never disappear from the
                # executor merely because its local retention clock advanced.
                # The control lifecycle owns desired-absence cleanup for these
                # explicitly allocated shared IDs.
                sessions = (
                    list(self._read_all_sessions_locked().values())
                    if session_id is not None
                    else self._prune_sessions_locked(now, reserve_slots=1)
                )
                maximum = int(self._settings().max_agent_sessions)
                if len(sessions) >= maximum:
                    raise RuntimeError(
                        "agent session limit reached: "
                        f"{maximum}; end an active session or wait for retention cleanup"
                    )
                if session_id is None:
                    for _ in range(16):
                        allocated_session_id = _generate_session_id()
                        if not self._metadata_path(
                            allocated_session_id
                        ).exists():
                            break
                    else:
                        raise RuntimeError(
                            "failed to allocate a unique session_id"
                        )
                else:
                    allocated_session_id = session_id
                    if self._metadata_path(allocated_session_id).exists():
                        raise ValueError(
                            f"session_id {allocated_session_id!r} already exists"
                        )
                session = AgentSession(
                    session_id=allocated_session_id,
                    target=target,
                    workdir=display_workdir,
                    machine=normalized_machine,
                    worker_session_id=normalized_worker_session_id,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                    label=label,
                    termination_requested_at=None,
                    persistent_shell_ids=(),
                )
                with self._state_store.transaction(
                    self._transaction_path(allocated_session_id)
                ):
                    self._write_session_locked(session)
                    return session

    def require_session(self, session_id: str) -> AgentSession:
        """Return an existing durable session or raise a clear error."""
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                return self._require_session_locked(session_id)

    def prepare_session_termination(self, session_id: str) -> AgentSession:
        """Make a known session cleanup-only even after its expiry boundary."""
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                session = self._require_session_locked(
                    session_id, allow_expired=True
                )
                now = time.time()
                updated = replace(
                    session,
                    updated_at=now,
                    expires_at=None,
                    termination_requested_at=(
                        session.termination_requested_at or now
                    ),
                )
                self._write_session_locked(updated)
                return updated

    def touch_session(self, session_id: str) -> AgentSession:
        """Refresh a session's durable updated timestamp."""
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                session = self._require_session_locked(session_id)
                updated = replace(session, updated_at=time.time())
                self._write_session_locked(updated)
                return updated

    def register_persistent_shell(
        self, session_id: str, shell_id: str
    ) -> AgentSession:
        """Durably bind one local persistent shell to its owning session."""
        normalized_shell_id = normalize_shell_id(shell_id)
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                session = self._require_session_locked(session_id)
                updated = bind_shell(
                    session,
                    normalized_shell_id,
                    updated_at=time.time(),
                )
                self._write_session_locked(updated)
                return updated

    def reserve_persistent_shell(
        self,
        session_id: str,
        shell_id: str,
        *,
        exclusive: bool = False,
    ) -> bool:
        """Durably reserve one shell id and report whether this call added it.

        Exclusive reservations reject ownership already recorded by another
        session. Persistent-shell admission holds its cross-process lock while
        using this mode, so the check and subsequent backend spawn are globally
        serialized.
        """
        normalized_shell_id = normalize_shell_id(shell_id)
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                session = self._require_session_locked(session_id)
                require_local_session(session)
                if exclusive:
                    ensure_shell_unowned(
                        session_id,
                        normalized_shell_id,
                        self._read_all_sessions_locked().values(),
                    )
                updated, added = reserve_shell(
                    session,
                    normalized_shell_id,
                    updated_at=time.time(),
                )
                if not added:
                    return False
                self._write_session_locked(updated)
                return True

    def release_session_persistent_shell(
        self, session_id: str, shell_id: str
    ) -> AgentSession:
        """Remove one shell id from exactly one durable session."""
        normalized_shell_id = normalize_shell_id(shell_id)
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                current = self._require_session_locked(
                    session_id, allow_expired=True
                )
                updated = release_shell(current, normalized_shell_id)
                if updated is current:
                    return current
                self._write_session_locked(updated)
                return updated

    def release_persistent_shell(self, shell_id: str) -> None:
        """Remove one persistent shell id from every durable local session."""
        normalized_shell_id = str(shell_id).strip()
        if not normalized_shell_id:
            return
        with self._lock:
            self._reset_for_current_root_locked()
            sessions = self._read_all_sessions_locked()
            for session in sessions.values():
                if normalized_shell_id not in session.persistent_shell_ids:
                    continue
                with self._state_store.transaction(
                    self._transaction_path(session.session_id)
                ):
                    current = self._load_session_locked(session.session_id)
                    if current is None:
                        continue
                    updated = release_shell(current, normalized_shell_id)
                    if updated is current:
                        continue
                    self._write_session_locked(updated)

    def persistent_shell_ids(self) -> set[str]:
        """Return all durable persistent shell ids without pruning sessions."""
        with self._lock:
            self._reset_for_current_root_locked()
            registry = self._state_store.layout.sessions_dir / ".registry"
            with self._state_store.transaction(registry):
                return collect_persistent_shell_ids(
                    self._read_all_sessions_locked().values()
                )

    def reconcile_session_persistent_shells(
        self, session_id: str, owned_shell_ids: set[str]
    ) -> AgentSession:
        """Replace one session's durable PTY ids with authoritative ownership."""
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                current = self._require_session_locked(
                    session_id, allow_expired=True
                )
                updated = replace_shells(current, owned_shell_ids)
                if updated is current:
                    return current
                self._write_session_locked(updated)
                return updated

    def reconcile_persistent_shells(self, live_shell_ids: set[str]) -> None:
        """Discard durable PTY ownership entries whose shell no longer exists."""
        live = {str(shell_id) for shell_id in live_shell_ids if shell_id}
        with self._lock:
            self._reset_for_current_root_locked()
            sessions = self._read_all_sessions_locked()
            for session in sessions.values():
                if all(
                    shell_id in live
                    for shell_id in session.persistent_shell_ids
                ):
                    continue
                with self._state_store.transaction(
                    self._transaction_path(session.session_id)
                ):
                    current = self._load_session_locked(session.session_id)
                    if current is None:
                        continue
                    updated = retain_live_shells(current, live)
                    if updated is current:
                        continue
                    self._write_session_locked(updated)

    def request_termination(self, session_id: str) -> AgentSession:
        """Persist an irreversible immediate-stop request for one session."""
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                session = self._require_session_locked(session_id)
                now = time.time()
                updated = replace(
                    session,
                    updated_at=now,
                    termination_requested_at=(
                        session.termination_requested_at or now
                    ),
                )
                self._write_session_locked(updated)
                return updated

    def admit_tool_sessions(
        self, session_ids: tuple[str, ...]
    ) -> tuple[AgentSession, ...]:
        """Admit active tool work and refresh activity only after all checks pass."""
        unique_session_ids = tuple(dict.fromkeys(session_ids))
        with self._lock:
            self._reset_for_current_root_locked()
            with ExitStack() as transactions:
                # Acquire every durable session lock in one stable order so
                # concurrent multi-session admissions cannot deadlock each
                # other. Keep all locks held through validation and refresh so
                # termination cannot become durable between those two steps.
                for session_id in sorted(unique_session_ids):
                    transactions.enter_context(
                        self._state_store.transaction(
                            self._transaction_path(session_id)
                        )
                    )

                sessions = tuple(
                    self._require_session_locked(session_id)
                    for session_id in unique_session_ids
                )
                for session in sessions:
                    if session.termination_requested_at is not None:
                        raise SessionTerminationRequestedError(
                            session.session_id
                        )

                now = time.time()
                admitted = tuple(
                    replace(session, updated_at=now) for session in sessions
                )
                for session in admitted:
                    self._write_session_locked(session)
                return admitted

    def admit_active_session(self, session_id: str) -> AgentSession:
        """Admit one active session using the authoritative tool-work policy."""
        return self.admit_tool_sessions((session_id,))[0]

    def session_cleanup_metadata(
        self, session_id: str
    ) -> tuple[float, bool, bool]:
        """Return activity/resource facts used by control-owned auto cleanup."""
        with self._lock:
            self._reset_for_current_root_locked()
            with (
                self._state_store.transaction(
                    self._transaction_path(session_id)
                ),
                self._state_store.transaction(
                    self._state_store.layout.jobs_lock_path
                ),
            ):
                session = self._require_session_locked(
                    session_id, allow_expired=True
                )
                active_job_session_ids = self._active_job_session_ids_locked()
                return (
                    session.updated_at,
                    bool(session.persistent_shell_ids),
                    self._session_has_active_jobs(
                        session_id, active_job_session_ids
                    ),
                )

    def require_cleanup_sessions(
        self, session_ids: tuple[str, ...]
    ) -> tuple[AgentSession, ...]:
        """Resolve known sessions for cleanup even after their expiry boundary."""
        sessions: list[AgentSession] = []
        for session_id in dict.fromkeys(session_ids):
            with self._lock:
                self._reset_for_current_root_locked()
                with self._state_store.transaction(
                    self._transaction_path(session_id)
                ):
                    sessions.append(
                        self._require_session_locked(
                            session_id, allow_expired=True
                        )
                    )
        return tuple(sessions)

    def assert_tool_call_allowed(
        self,
        session_ids: tuple[str, ...],
        *,
        termination_cleanup: bool = False,
    ) -> None:
        """Compatibility facade over active admission or cleanup lookup."""
        if termination_cleanup:
            self.require_cleanup_sessions(session_ids)
            return
        self.admit_tool_sessions(session_ids)

    def change_session_workdir(
        self, session_id: str, workdir: str | Path
    ) -> AgentSession:
        """Update a local session workdir and clear its durable snapshots."""
        resolved_workdir = resolve_path(workdir, must_exist=True)
        if not resolved_workdir.is_dir():
            raise NotADirectoryError(str(resolved_workdir))
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                session = self._require_session_locked(session_id)
                if session.target != "local":
                    raise ValueError(
                        "remote session cwd changes are not available"
                    )
                updated = replace(
                    session,
                    workdir=str(resolved_workdir),
                    updated_at=time.time(),
                )
                # Grounding for the old cwd must be gone before the durable cwd
                # can point anywhere else. A crash between these two mutations
                # therefore leaves either old cwd + no snapshots, or new cwd +
                # no old snapshots.
                self._snapshot_repository.remove_session(session_id)
                self._write_session_locked(updated)
                return updated

    def update_remote_session_workdir(
        self, session_id: str, workdir: str
    ) -> AgentSession:
        """Cache a worker-validated remote workdir and clear durable snapshots."""
        if not workdir:
            raise ValueError("remote workdir must not be empty")
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                session = self._require_session_locked(session_id)
                if session.target != "remote":
                    raise ValueError("session is not remote")
                updated = replace(
                    session,
                    workdir=workdir,
                    updated_at=time.time(),
                )
                self._snapshot_repository.remove_session(session_id)
                self._write_session_locked(updated)
                return updated

    def end_session(self, session_id: str) -> AgentSession:
        """Remove one durable session and all state owned by it."""
        with self._lock:
            self._reset_for_current_root_locked()
            registry = self._state_store.layout.sessions_dir / ".registry"
            with (
                self._state_store.transaction(registry),
                self._state_store.transaction(
                    self._transaction_path(session_id)
                ),
            ):
                session = self._require_session_locked(
                    session_id, allow_expired=True
                )
                self._remove_session_state_locked(session_id)
                return session

    def list_sessions(self) -> list[AgentSession]:
        """Return all durable sessions ordered by most recent activity."""
        with self._lock:
            self._reset_for_current_root_locked()
            registry = self._state_store.layout.sessions_dir / ".registry"
            with self._state_store.transaction(registry):
                return self._prune_sessions_locked(time.time())

    def record_file_snapshot(
        self,
        *,
        session_id: str,
        path: str,
        file_sha256: str,
        total_lines: int,
        seen_ranges: tuple[tuple[int, int], ...],
    ) -> SnapshotRecord:
        """Durably record a read/search-visible file snapshot."""
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                self._require_session_locked(session_id)
                return self._snapshot_repository.record(
                    session_id=session_id,
                    snapshot_id=new_snapshot_id(),
                    path=path,
                    file_sha256=file_sha256,
                    total_lines=total_lines,
                    seen_ranges=seen_ranges,
                    created_at=time.time(),
                )

    def get_snapshot(
        self, session_id: str, snapshot_id: str
    ) -> SnapshotRecord | None:
        """Return a durable snapshot for a session, if it still exists."""
        with self._lock:
            self._reset_for_current_root_locked()
            with self._state_store.transaction(
                self._transaction_path(session_id)
            ):
                self._require_session_locked(session_id)
                return self._snapshot_repository.get(session_id, snapshot_id)

    def clear(self) -> None:
        """Clear all durable and in-process session state. Intended for tests."""
        with self._lock:
            self._reset_for_current_root_locked()
            self._state_store.remove(
                self._state_store.layout.sessions_dir, recursive=True
            )
            self._session_repository.clear_cache()
            self._snapshot_repository.clear_cache()


def resolve_session_path(
    session: AgentSession,
    path: str | Path,
    *,
    must_exist: bool = False,
    allow_missing_parent: bool = True,
    follow_final_symlink: bool = True,
) -> Path:
    """Resolve a path relative to a local session workdir and enforce containment."""
    if session.target != "local":
        raise ValueError("remote sessions are not dispatchable locally yet")
    workdir = Path(session.workdir).resolve()
    raw = Path(path)
    candidate = raw if raw.is_absolute() else workdir / raw
    resolved = resolve_path(
        candidate,
        must_exist=must_exist,
        allow_missing_parent=allow_missing_parent,
        follow_final_symlink=follow_final_symlink,
    )
    boundary = resolved if follow_final_symlink else resolved.parent
    try:
        boundary.relative_to(workdir)
    except ValueError as exc:
        raise ValueError(f"Path escapes session workdir: {path}") from exc
    return resolved


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of a file without loading it all at once."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_input_session_ids(value: Any) -> tuple[str, ...]:
    """Extract session ids only from semantic tool-argument positions."""
    found: list[str] = []
    seen: set[int] = set()
    session_names = (
        "session_id",
        "src_session_id",
        "dst_session_id",
        "source_session_id",
        "destination_session_id",
    )
    argument_envelopes = ("kwargs", "keyword_args")

    def visit(candidate: Any) -> None:
        if not isinstance(candidate, Mapping):
            return
        identity = id(candidate)
        if identity in seen:
            return
        seen.add(identity)
        for name in session_names:
            child = candidate.get(name)
            if isinstance(child, str) and child:
                found.append(child)
        for name in argument_envelopes:
            visit(candidate.get(name))

    visit(value)
    return tuple(dict.fromkeys(found))


def enforce_tool_session_control(
    value: Any, *, termination_cleanup: bool = False
) -> None:
    """Apply persisted immediate-stop policy to one model tool invocation."""
    session_ids = tool_input_session_ids(value)
    if not session_ids:
        return
    store = get_tool_session_store()
    if termination_cleanup:
        store.require_cleanup_sessions(session_ids)
        return
    store.admit_tool_sessions(session_ids)


_STORE: ToolSessionStore | None = None


def configure_tool_session_store(
    store: ToolSessionStore | None,
) -> ToolSessionStore | None:
    """Install a process-wide tool-session store and return the previous binding."""
    global _STORE
    previous = _STORE
    _STORE = store
    return previous


def get_tool_session_store() -> ToolSessionStore:
    """Return the configured session store, with a compatibility lazy fallback."""
    global _STORE
    if _STORE is None:
        _STORE = ToolSessionStore()
    return _STORE
