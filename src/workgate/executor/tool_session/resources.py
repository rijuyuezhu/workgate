"""Pure persistent-resource ownership policy for explicit tool sessions."""

from collections.abc import Iterable
from dataclasses import replace

from .records import AgentSession


def normalize_shell_id(shell_id: str) -> str:
    """Return one non-empty normalized persistent-shell id."""
    normalized = str(shell_id).strip()
    if not normalized:
        raise ValueError("shell_id must not be empty")
    return normalized


def bind_shell(
    session: AgentSession,
    shell_id: str,
    *,
    updated_at: float,
) -> AgentSession:
    """Bind one shell id while preserving insertion order and refreshing activity."""
    normalized = normalize_shell_id(shell_id)
    return replace(
        session,
        updated_at=updated_at,
        persistent_shell_ids=tuple(
            dict.fromkeys((*session.persistent_shell_ids, normalized))
        ),
    )


def ensure_shell_unowned(
    session_id: str,
    shell_id: str,
    sessions: Iterable[AgentSession],
) -> None:
    """Reject a shell id already owned by a different durable session."""
    normalized = normalize_shell_id(shell_id)
    for other in sessions:
        if (
            other.session_id != session_id
            and normalized in other.persistent_shell_ids
        ):
            raise RuntimeError(
                "persistent shell id is already reserved by another session: "
                f"{normalized}"
            )


def reserve_shell(
    session: AgentSession,
    shell_id: str,
    *,
    updated_at: float,
) -> tuple[AgentSession, bool]:
    """Reserve one shell id and report whether the durable row changed."""
    normalized = normalize_shell_id(shell_id)
    if normalized in session.persistent_shell_ids:
        return session, False
    return (
        replace(
            session,
            updated_at=updated_at,
            persistent_shell_ids=(*session.persistent_shell_ids, normalized),
        ),
        True,
    )


def release_shell(session: AgentSession, shell_id: str) -> AgentSession:
    """Remove one shell id from one session without changing activity time."""
    normalized = normalize_shell_id(shell_id)
    retained = tuple(
        existing
        for existing in session.persistent_shell_ids
        if existing != normalized
    )
    if retained == session.persistent_shell_ids:
        return session
    return replace(session, persistent_shell_ids=retained)


def replace_shells(
    session: AgentSession, owned_shell_ids: set[str]
) -> AgentSession:
    """Replace one session's ownership with an authoritative shell set."""
    normalized = tuple(
        sorted(
            {
                str(shell_id).strip()
                for shell_id in owned_shell_ids
                if str(shell_id).strip()
            }
        )
    )
    if session.persistent_shell_ids == normalized:
        return session
    return replace(session, persistent_shell_ids=normalized)


def retain_live_shells(
    session: AgentSession, live_shell_ids: set[str]
) -> AgentSession:
    """Drop ownership for shells absent from an authoritative live inventory."""
    live = {str(shell_id) for shell_id in live_shell_ids if shell_id}
    retained = tuple(
        shell_id
        for shell_id in session.persistent_shell_ids
        if shell_id in live
    )
    if retained == session.persistent_shell_ids:
        return session
    return replace(session, persistent_shell_ids=retained)


def persistent_shell_ids(sessions: Iterable[AgentSession]) -> set[str]:
    """Return the union of durable persistent-shell ids."""
    return {
        shell_id
        for session in sessions
        for shell_id in session.persistent_shell_ids
    }
