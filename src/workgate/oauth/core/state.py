"""Controller-owned OAuth authorization and dynamic-client live state."""

from collections.abc import Mapping
from pathlib import Path
from threading import RLock

from ...persistence import FileStateStore, StateStore
from .client_store import load_persisted_clients
from .models import AuthCode, OAuthClient


class OAuthState:
    """Own process-local OAuth clients, authorization codes, and their locks."""

    def __init__(
        self, state_dir: Path, *, state_store: StateStore | None = None
    ) -> None:
        self.state_dir = state_dir
        self.state_store = state_store or FileStateStore(lambda: state_dir)
        self.clients: dict[str, OAuthClient] = {}
        self.codes: dict[str, AuthCode] = {}
        self.client_lock = RLock()
        self.code_lock = RLock()
        self._started = False
        self._accepting = True
        self._closed = False

    def require_open(self) -> None:
        """Reject new OAuth work after the owning runtime begins shutdown."""
        if self._closed or (self._started and not self._accepting):
            raise RuntimeError("OAuth state is shutting down")

    def start(self) -> int:
        """Load durable approved clients and begin the owning lifecycle."""
        if self._closed:
            raise RuntimeError("OAuthState cannot be restarted after close")
        if self._started:
            return 0
        with self.client_lock:
            staged_clients = dict(self.clients)
            loaded = load_persisted_clients(
                staged_clients, state_store=self.state_store
            )
            self.clients.clear()
            self.clients.update(staged_clients)
        self._accepting = True
        self._started = True
        return loaded

    def stop_admission(self) -> None:
        """Prevent new OAuth mutations while allowing current lock holders to drain."""
        self._accepting = False

    async def aclose(self) -> None:
        """Stop admission and discard all process-local OAuth working state."""
        if self._closed:
            return
        self.stop_admission()
        # Authorization-code issuance already nests code_lock -> client_lock.
        # Keep that order here so shutdown cannot deadlock with an in-flight issue.
        with self.code_lock, self.client_lock:
            self.codes.clear()
            self.clients.clear()
            self._closed = True

    def snapshot_clients(self) -> Mapping[str, OAuthClient]:
        """Return the owned client mapping for diagnostics and tests."""
        return self.clients

    def snapshot_codes(self) -> Mapping[str, AuthCode]:
        """Return the owned authorization-code mapping for diagnostics and tests."""
        return self.codes


_OAUTH_STATE: OAuthState | None = None


def configure_oauth_state(state: OAuthState | None) -> OAuthState | None:
    """Install a non-owning compatibility binding and return the previous binding."""
    global _OAUTH_STATE
    previous = _OAUTH_STATE
    _OAUTH_STATE = state
    return previous


def oauth_state() -> OAuthState:
    """Return the OAuth state bound by the current controller/test composition root."""
    if _OAUTH_STATE is None:
        raise RuntimeError("OAuth state is not configured")
    return _OAUTH_STATE


def build_oauth_state(
    state_dir: Path, *, state_store: StateStore | None = None
) -> OAuthState:
    """Construct OAuth live state without installing process compatibility bindings."""
    return OAuthState(state_dir, state_store=state_store)
