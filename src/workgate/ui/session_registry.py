"""Renewable browser-session state adapted from final upstream v6.0.3.

Source: f0ecf8f8039345f24d0de4402983766787f4574e,
webui/auth.py (6c45a247603a559aac56bf877ccf600138efe812).
"""

import hashlib
import time

from ..persistence import StateStore

SESSION_TTL = 30 * 24 * 60 * 60
_MAX_SESSIONS = 4096
_MAX_STORE_BYTES = 1024 * 1024


class WebAuthStore:
    """Store only hashed renewable session identities and bounded deadlines.

    The signed native browser cookie already carries owner, scope, origin and
    binding information. Use the existing private Control state transaction
    rather than create a second login-ticket or Chat database dependency.
    """

    def __init__(self, store: StateStore):
        self.store = store
        self.path = store.layout.ui_sessions_path

    @staticmethod
    def _hash(identity: str) -> str:
        return hashlib.sha256(identity.encode("ascii")).hexdigest()

    def _read(self) -> dict[str, int]:
        value = self.store.read_json(self.path, max_bytes=_MAX_STORE_BYTES)
        if value is None:
            return {}
        if (
            not isinstance(value, dict)
            or len(value) > _MAX_SESSIONS
            or any(
                not isinstance(key, str)
                or len(key) != 64
                or type(expiry) is not int
                for key, expiry in value.items()
            )
        ):
            raise ValueError("Invalid browser session registry")
        return value

    def issue(self, identity: str, expires_at: int) -> None:
        now = int(time.time())
        if expires_at <= now or expires_at > now + SESSION_TTL:
            raise ValueError("Invalid browser session deadline")
        with self.store.transaction(self.path):
            records = {
                key: expiry
                for key, expiry in self._read().items()
                if expiry > now
            }
            key = self._hash(identity)
            if key in records or len(records) >= _MAX_SESSIONS:
                raise ValueError("Browser session capacity reached")
            records[key] = expires_at
            self.store.write_json(self.path, records)

    def session(self, identity: str) -> int | None:
        with self.store.transaction(self.path):
            expiry = self._read().get(self._hash(identity))
            return (
                expiry
                if expiry is not None and expiry > int(time.time())
                else None
            )

    def renew(self, identity: str) -> int | None:
        """Extend only a live session, never resurrect an expired/revoked one."""
        now = int(time.time())
        until = now + SESSION_TTL
        with self.store.transaction(self.path):
            records = self._read()
            key = self._hash(identity)
            if records.get(key, 0) <= now:
                return None
            records[key] = until
            self.store.write_json(self.path, records)
            return until

    def revoke(self, identity: str) -> None:
        with self.store.transaction(self.path):
            records = self._read()
            if records.pop(self._hash(identity), None) is not None:
                self.store.write_json(self.path, records)
