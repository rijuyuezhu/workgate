"""Renewable browser-session state adapted from final upstream v6.0.3.

Source: f0ecf8f8039345f24d0de4402983766787f4574e,
webui/auth.py (6c45a247603a559aac56bf877ccf600138efe812).
"""

class WebAuthStore:
    def __init__(self, directory: Path):
        from ..sqlite_store import shared_database

        self.database = shared_database(directory.resolve())

    def issue_ticket(self, owner: str, scopes: str, grant_until: int, *, renewable: bool = False) -> str:
        now = int(time.time())
        if not owner or grant_until <= now:
            raise ValueError("An active authenticated identity is required")
        secret = secrets.token_urlsafe(32)
        with self.database.connect() as db:
            db.execute("DELETE FROM web_login_tickets WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO web_login_tickets(hash,owner,scopes,grant_until,expires_at,renewable) VALUES(?,?,?,?,?,?)",
                       (_hash(secret), owner, scopes, grant_until, now + TICKET_TTL, int(renewable)))
        return secret

    def redeem(self, ticket: str) -> tuple[str, WebSession]:
        if len(ticket) > 256 or len(ticket) < 32:
            raise ValueError("Invalid login ticket")
        now = int(time.time())
        with self.database.connect() as db:
            db.execute("DELETE FROM web_sessions WHERE expires_at<=?", (now,))
            row = db.execute("DELETE FROM web_login_tickets WHERE hash=? RETURNING owner,scopes,grant_until,expires_at,renewable",
                             (_hash(ticket),)).fetchone()
            if not row or row["expires_at"] < now or row["grant_until"] < now:
                raise ValueError("Invalid or expired login ticket")
            raw = secrets.token_urlsafe(48)
            expiry = min(row["grant_until"], now + SESSION_TTL)
            db.execute("INSERT INTO web_sessions(hash,owner,scopes,grant_until,expires_at,renewable) VALUES(?,?,?,?,?,?)",
                       (_hash(raw), row["owner"], row["scopes"], row["grant_until"], expiry, row["renewable"]))
            return raw, WebSession(row["owner"], row["scopes"], row["grant_until"])

    def session(self, cookie: str) -> WebSession | None:
        if not cookie or len(cookie) > 256:
            return None
        now = int(time.time())
        with self.database.connect() as db:
            row = db.execute("SELECT owner,scopes,grant_until,expires_at FROM web_sessions WHERE hash=?",
                             (_hash(cookie),)).fetchone()
            if not row or row["expires_at"] <= now or row["grant_until"] <= now:
                return None
            return WebSession(row["owner"], row["scopes"], row["grant_until"])

    def renew(self, cookie: str) -> WebSession | None:
        """Slide an authenticated browser session on actual frontend activity.

        A revoked or expired cookie is never resurrected. The cookie itself is
        the revocable, HttpOnly browser credential; external OAuth tokens keep
        their own independent, shorter expiry.
        """
        if not cookie or len(cookie) > 256:
            return None
        now = int(time.time())
        until = now + SESSION_TTL
        with self.database.connect() as db:
            row = db.execute(
                "UPDATE web_sessions SET expires_at=?,grant_until=? "
                "WHERE hash=? AND expires_at>? AND grant_until>? AND renewable=1 "
                "RETURNING owner,scopes,grant_until",
                (until, until, _hash(cookie), now, now),
            ).fetchone()
            if row is None:
                # Legacy and explicitly bounded OAuth grants stay valid, but
                # no independent connection or 30-day promotion is necessary.
                row = db.execute(
                    "SELECT owner,scopes,grant_until FROM web_sessions "
                    "WHERE hash=? AND expires_at>? AND grant_until>?",
                    (_hash(cookie), now, now),
                ).fetchone()
            return (WebSession(row["owner"], row["scopes"], row["grant_until"])
                    if row is not None else None)

    def revoke(self, cookie: str) -> None:
        with self.database.connect() as db:
            db.execute("DELETE FROM web_sessions WHERE hash=?", (_hash(cookie),))

