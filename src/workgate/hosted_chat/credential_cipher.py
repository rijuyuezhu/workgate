"""Shared private credential envelope extracted from final upstream Actions/OAuth.

Source: upstream v6.0.3 (f0ecf8f8039345f24d0de4402983766787f4574e),
webui/actions.py _cipher (70c9c60616457103ebfe981131eebe51d18a7fda),
webui/model_oauth.py seal/unseal (fab8ab54c6e84a9d1caaeefa75bb6c16d622b00f).
See LICENSE.upstream.

Initialization records a key fingerprint in SQLite so a missing or replaced key
is rejected even on restart. Reading or writing never regenerates a key.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..utils.private_files import atomic_write_private_bytes, private_file_lock
from .database import SQLiteDatabase

_KEY_OWNER = "__workgate_controller__"
_KEY_ID = "credentials.key.sha256"


class CredentialUnavailable(ValueError):
    """The credential cannot be read with the configured key and binding."""


class CredentialCipher:
    def __init__(
        self, database: SQLiteDatabase, *, supplied_key: bytes | None = None
    ) -> None:
        self.database = database
        self.path = Path(database.root) / "credentials-fernet.key"
        self.lock = self.path.with_suffix(".lock")
        self._supplied_key = supplied_key

    def _load_key(self, *, initialize: bool = False) -> bytes:
        try:
            if self._supplied_key is not None:
                key = self._supplied_key
            else:
                SQLiteDatabase._verify_private_file(self.lock, optional=True)
                with private_file_lock(self.lock, timeout_s=30):
                    SQLiteDatabase._verify_private_file(
                        self.path, optional=initialize
                    )
                    if initialize and not self.path.exists():
                        atomic_write_private_bytes(
                            self.path, Fernet.generate_key()
                        )
                    with self.path.open("rb") as stream:
                        key = stream.read(45)
            if len(key) != 44:
                raise ValueError("Invalid Fernet key length")
            Fernet(key)
            return key
        except OSError, ValueError, TypeError:
            raise CredentialUnavailable(
                "Hosted Chat credential key is unavailable"
            ) from None

    @staticmethod
    def _key_id(db: sqlite3.Connection) -> str | None:
        row = db.execute(
            "SELECT value FROM settings WHERE owner=? AND key=?",
            (_KEY_OWNER, _KEY_ID),
        ).fetchone()
        return json.loads(row[0]) if row is not None else None

    @staticmethod
    def _check_key(key: bytes, expected: str | None) -> None:
        if hashlib.sha256(key).hexdigest() != expected:
            raise CredentialUnavailable(
                "Hosted Chat credential key does not match the store"
            )

    def initialize(self) -> None:
        """Provision once, or verify the existing key; never replace a lost key."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expected = self._key_id(db)
            key = self._load_key(initialize=expected is None)
            if expected is not None:
                self._check_key(key, expected)
            else:
                db.execute(
                    "INSERT INTO settings(owner,key,value) VALUES(?,?,?)",
                    (
                        _KEY_OWNER,
                        _KEY_ID,
                        json.dumps(hashlib.sha256(key).hexdigest()),
                    ),
                )

    def _cipher(self) -> Fernet:
        key = self._load_key()
        with self.database.read_snapshot() as db:
            self._check_key(key, self._key_id(db))
        return Fernet(key)

    def seal(self, value: dict, *, binding: str) -> str:
        return (
            self._cipher()
            .encrypt(json.dumps({"binding": binding, "value": value}).encode())
            .decode()
        )

    def unseal(self, token: str, *, binding: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._cipher().decrypt(token.encode()))
            if (
                not isinstance(payload, dict)
                or payload.get("binding") != binding
                or not isinstance(payload.get("value"), dict)
            ):
                raise ValueError("Credential binding mismatch")
            return payload["value"]
        except AttributeError, ValueError, InvalidToken, TypeError:
            raise CredentialUnavailable(
                "Hosted Chat credentials are unavailable"
            ) from None
