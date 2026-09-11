"""Per-operation immutable views of executor-owned shared sessions."""

from dataclasses import dataclass

from .records import AgentSession, valid_session_id


@dataclass(frozen=True)
class SessionBinding:
    """Validated executor-local coordinates for one shared-session operation."""

    session_id: str
    """Shared control/executor session identifier."""
    workdir: str
    """Authoritative executor-side workdir snapshot for this operation."""


def binding_from_record(session: AgentSession) -> SessionBinding:
    """Purely validate and convert one durable record into an operation binding."""
    if valid_session_id(session.session_id) is None:
        raise ValueError("session record contains an invalid session_id")
    if not session.workdir:
        raise ValueError("session record contains an empty workdir")
    return SessionBinding(
        session_id=session.session_id, workdir=session.workdir
    )
