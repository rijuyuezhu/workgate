"""Authoritative per-operation resolution of typed session bindings."""

from .bindings import SessionBinding, binding_from_record
from .store import ToolSessionStore


class SessionResolver:
    """Resolve fresh operation bindings without caching durable session state."""

    def __init__(self, store: ToolSessionStore) -> None:
        self._store = store

    def resolve_active_binding(self, session_id: str) -> SessionBinding:
        """Admit active work, then convert the admitted record to a binding."""
        return binding_from_record(self._store.admit_active_session(session_id))

    def prepare_cleanup_binding(self, session_id: str) -> SessionBinding:
        """Prepare even an expired session for teardown, then return its binding."""
        return binding_from_record(
            self._store.prepare_session_termination(session_id)
        )
