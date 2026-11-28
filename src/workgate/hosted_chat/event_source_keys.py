"""External Agent event source credentials, distinct from Control OAuth."""

import hashlib
import json
import re
import secrets
import time

from .database import SQLiteDatabase

_TOKEN = re.compile(r"lsm_evt_[A-Za-z0-9_-]{64}")
_ID = re.compile(r"eventkey_[a-f0-9]{32}")
_MODES = frozenset({"fire", "continue", "steer"})
_MAX_KEYS = 1000


def _now() -> int:
    return time.time_ns() // 1_000_000


def live_source(
    db, owner: str, key_id: str, *, mode: str | None = None
) -> bool:
    row = db.execute(
        "SELECT permissions,expires_at,revoked_at FROM agent_event_source_keys WHERE owner=? AND id=?",
        (owner, key_id),
    ).fetchone()
    return bool(
        row is not None
        and row["revoked_at"] is None
        and (row["expires_at"] is None or row["expires_at"] > _now())
        and (mode is None or mode in json.loads(row["permissions"]))
    )


class EventSourceKeys:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def mint(
        self,
        owner: str,
        *,
        label: str = "event-source",
        permissions=("fire", "continue", "steer"),
        expires_at: int | None = None,
    ) -> dict:
        if (
            not isinstance(owner, str)
            or not owner
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 128
        ):
            raise ValueError(
                "A nonempty event source owner and label are required"
            )
        if (
            not isinstance(permissions, list | tuple)
            or not 1 <= len(permissions) <= 3
            or any(
                not isinstance(mode, str) or mode not in _MODES
                for mode in permissions
            )
        ):
            raise ValueError("Invalid event source permissions")
        if expires_at is not None and (
            type(expires_at) is not int or not _now() < expires_at <= 2**53 - 1
        ):
            raise ValueError("Event source expiration must be in the future")
        modes = sorted(set(permissions))
        token = "lsm_evt_" + secrets.token_urlsafe(48)
        key_id = "eventkey_" + secrets.token_hex(16)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT COUNT(*) FROM agent_event_source_keys WHERE owner=?",
                (owner,),
            ).fetchone()[0]
            if count >= _MAX_KEYS:
                raise ValueError("Event source key limit reached")
            db.execute(
                "INSERT INTO agent_event_source_keys(id,owner,label,token_hash,permissions,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    key_id,
                    owner,
                    label,
                    hashlib.sha256(token.encode()).hexdigest(),
                    json.dumps(modes),
                    expires_at,
                    _now(),
                ),
            )
        return {
            "id": key_id,
            "label": label,
            "token": token,
            "permissions": modes,
            "expiresAt": expires_at,
        }

    def authenticate(self, token: object) -> dict | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        with self.database.read_snapshot() as db:
            row = db.execute(
                "SELECT id,owner,permissions FROM agent_event_source_keys WHERE token_hash=? "
                "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                (hashlib.sha256(token.encode()).hexdigest(), _now()),
            ).fetchone()
        return (
            {
                "id": row["id"],
                "owner": row["owner"],
                "permissions": json.loads(row["permissions"]),
            }
            if row
            else None
        )

    def list(self, owner: str) -> list[dict]:
        with self.database.read_snapshot() as db:
            rows = db.execute(
                "SELECT id,label,permissions,expires_at,revoked_at,created_at FROM agent_event_source_keys WHERE owner=? ORDER BY created_at DESC,id LIMIT ?",
                (owner, _MAX_KEYS),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "label": row["label"],
                "permissions": json.loads(row["permissions"]),
                "expiresAt": row["expires_at"],
                "revokedAt": row["revoked_at"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def revoke(self, owner: str, key_id: str) -> bool:
        if not isinstance(key_id, str) or not _ID.fullmatch(key_id):
            return False
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            updated = db.execute(
                "UPDATE agent_event_source_keys SET revoked_at=? WHERE id=? AND owner=? AND revoked_at IS NULL",
                (now, key_id, owner),
            ).rowcount
            if updated:
                # Future event execution must check this source fence in the
                # same transaction as claim/renewal. No native grants exist.
                db.execute(
                    "UPDATE agent_event_deliveries SET status='dead',error_code='SOURCE_REVOKED',error_type='PermissionError',lease_owner=NULL,lease_until=NULL,updated_at=? "
                    "WHERE owner=? AND source_key_id=? AND status IN ('pending','leased')",
                    (now, owner, key_id),
                )
            return updated == 1
