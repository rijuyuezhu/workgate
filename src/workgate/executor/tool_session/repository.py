"""Durable metadata repository for explicit tool sessions."""

from pathlib import Path

from ...persistence import StateStore
from .records import AgentSession, session_from_payload, session_to_payload


class SessionRepository:
    """Own durable session metadata paths, cache, and filesystem mechanics."""

    def __init__(
        self, state_store: StateStore, *, metadata_max_bytes: int
    ) -> None:
        self._state_store = state_store
        self._metadata_max_bytes = metadata_max_bytes
        self._loaded_root: Path | None = None
        self._sessions: dict[str, AgentSession] = {}

    def reset_for_current_root(self) -> bool:
        """Reset process-local metadata when the configured state root changes."""
        root = self._state_store.layout.root
        if self._loaded_root == root:
            return False
        self._sessions.clear()
        self._loaded_root = root
        return True

    def clear_cache(self) -> None:
        """Drop all process-local session metadata without touching durable files."""
        self._sessions.clear()

    def replace_cache(self, sessions: dict[str, AgentSession]) -> None:
        """Replace the process-local cache with one authoritative snapshot."""
        self._sessions = dict(sessions)

    def metadata_path(self, session_id: str) -> Path:
        """Return the canonical metadata path for one explicit session."""
        return self._state_store.layout.session_metadata_path(session_id)

    def transaction_path(self, session_id: str) -> Path:
        """Return the canonical transaction key for one explicit session."""
        return self._state_store.layout.session_transaction_path(session_id)

    def metadata_exists(self, session_id: str) -> bool:
        """Return whether one durable session metadata file exists."""
        return self.metadata_path(session_id).exists()

    def remove(self, session_id: str) -> None:
        """Remove one session directory and its process-local cached metadata."""
        self._state_store.remove(
            self._state_store.layout.session_dir(session_id),
            recursive=True,
        )
        self._sessions.pop(session_id, None)

    def read_all(self) -> dict[str, AgentSession]:
        """Load valid durable session metadata without applying retention policy."""
        sessions: dict[str, AgentSession] = {}
        for directory in self._state_store.iter_directories(
            self._state_store.layout.sessions_dir
        ):
            try:
                session = session_from_payload(
                    self._state_store.read_json(
                        directory / "session.json",
                        max_bytes=self._metadata_max_bytes,
                    )
                )
            except OSError, TypeError, ValueError:
                continue
            if session.session_id == directory.name:
                sessions[session.session_id] = session
        return sessions

    def load(self, session_id: str) -> AgentSession | None:
        """Load one session from durable metadata and refresh its cache entry."""
        value = self._state_store.read_json(
            self.metadata_path(session_id),
            max_bytes=self._metadata_max_bytes,
        )
        if value is None:
            self._sessions.pop(session_id, None)
            return None
        session = session_from_payload(value)
        if session.session_id != session_id:
            raise ValueError("session directory and metadata id do not match")
        self._sessions[session_id] = session
        return session

    def write(self, session: AgentSession) -> None:
        """Persist one session record and refresh the process-local cache."""
        self._state_store.write_json(
            self.metadata_path(session.session_id), session_to_payload(session)
        )
        self._sessions[session.session_id] = session
