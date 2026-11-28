"""Version-bound OAuth token exchange and rotation for owned Actions.

Adapted from pinned upstream webui/action_oauth.py. Use the existing
origin-pinned transport and private credential cipher; no network retry.
"""

import asyncio
import base64
import json
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .action_runtime import ActionHTTPTransport
from .actions import ActionRepository
from .credential_cipher import CredentialUnavailable

_TOKEN_LIMIT = 32 * 1024
_TOKEN_TIMEOUT = 20.0
_REFRESH_LEASE = 60
_REFRESH_WAIT = 45
_REFRESH_KIND = "refresh_claim"


def _validated_token(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid OAuth token response")
    kind = value.get("token_type", "Bearer")
    if not isinstance(kind, str) or kind.lower() != "bearer":
        raise ValueError("Unsupported OAuth token type")
    result = {}
    for field in ("access_token", "refresh_token"):
        token = value.get(field)
        if token is None and field == "refresh_token":
            continue
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 16384
            or any(ord(c) < 32 or ord(c) == 127 for c in token)
        ):
            raise ValueError("Invalid OAuth token")
        result[field] = token
    for field in ("expires_in", "refresh_token_expires_in"):
        expiry = value.get(field)
        if expiry is None:
            continue
        if type(expiry) is int:
            seconds = expiry
        elif (
            isinstance(expiry, str)
            and expiry.isascii()
            and expiry.isdigit()
            and len(expiry) <= 10
        ):
            seconds = int(expiry)
        else:
            raise ValueError("Invalid OAuth token expiry")
        if not 0 <= seconds <= 10 * 365 * 86400:
            raise ValueError("Invalid OAuth token expiry")
        result[field] = seconds
    return result


async def exchange(
    endpoint: str, body: dict[str, str], credentials: dict, method: str
) -> dict:
    """Exchange a code or refresh token without exposing request secrets."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Invalid OAuth token URL")
    transport_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if method == "basic_auth_header":
        raw = (
            credentials["oauth_client_id"]
            + ":"
            + credentials["oauth_client_secret"]
        ).encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    elif method == "default_post":
        body = {
            **body,
            "client_id": credentials["oauth_client_id"],
            "client_secret": credentials["oauth_client_secret"],
        }
    else:
        raise ValueError("Invalid OAuth token exchange method")
    try:
        async with asyncio.timeout(_TOKEN_TIMEOUT):
            async with (
                httpx.AsyncClient(
                    transport=ActionHTTPTransport(transport_url),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=_TOKEN_TIMEOUT,
                ) as client,
                client.stream(
                    "POST", endpoint, data=body, headers=headers
                ) as response,
            ):
                if response.status_code != 200:
                    raise PermissionError("OAuth token exchange rejected")
                if (
                    response.headers.get("content-encoding", "identity").lower()
                    != "identity"
                ):
                    raise ValueError("Unsupported OAuth response encoding")
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > _TOKEN_LIMIT:
                        raise ValueError("OAuth token response too large")
                    raw.extend(chunk)
        return _validated_token(json.loads(raw))
    except httpx.RequestError, TimeoutError:
        raise ConnectionError("OAuth token endpoint unavailable") from None
    except json.JSONDecodeError, UnicodeDecodeError, RecursionError:
        raise ValueError("Invalid OAuth token response") from None


class ActionOAuthTokens:
    def __init__(self, repository: ActionRepository):
        self.repo = repository
        self.database = repository.database

    def _action(self, db, owner: str, action_id: str, version: int):
        row = db.execute(
            "SELECT * FROM action_records WHERE id=? AND owner=? AND version=?",
            (action_id, owner, version),
        ).fetchone()
        if (
            row is None
            or json.loads(row["metadata"]).get("auth", {}).get("type")
            != "oauth"
        ):
            raise PermissionError("OAuth Action is unavailable or changed")
        return row

    @staticmethod
    def _binding(row, kind: str) -> str:
        return json.dumps(
            [
                "action-oauth",
                row["owner"],
                row["agent_id"],
                row["id"],
                row["version"],
                kind,
            ]
        )

    def _row(self, db, action, kind):
        return db.execute(
            "SELECT token,expires_at FROM action_oauth_tokens WHERE action_id=? AND owner=? AND kind=?",
            (action["id"], action["owner"], kind),
        ).fetchone()

    def _token(self, db, action, kind, now):
        row = self._row(db, action, kind)
        if row is None or (
            row["expires_at"] is not None
            and row["expires_at"] <= now + (5 if kind == "access" else 0)
        ):
            return None
        value = self.repo.cipher.unseal(
            row["token"], binding=self._binding(action, kind)
        )
        token = value.get("token")
        if not isinstance(token, str) or not token:
            raise CredentialUnavailable("OAuth Action credentials unavailable")
        return token

    def _clear(self, db, action):
        db.execute(
            "DELETE FROM action_oauth_tokens WHERE owner=? AND action_id=?",
            (action["owner"], action["id"]),
        )

    def _store(
        self, db, action, result: dict, *, now: int, preserve_refresh: bool
    ):
        result = _validated_token(result)
        for kind, field, expiry_field in (
            ("access", "access_token", "expires_in"),
            ("refresh", "refresh_token", "refresh_token_expires_in"),
        ):
            if field not in result:
                if kind == "refresh" and not preserve_refresh:
                    db.execute(
                        "DELETE FROM action_oauth_tokens WHERE owner=? AND action_id=? AND kind='refresh'",
                        (action["owner"], action["id"]),
                    )
                continue
            sealed = self.repo.cipher.seal(
                {"token": result[field]}, binding=self._binding(action, kind)
            )
            expires = (
                now + result[expiry_field] if expiry_field in result else None
            )
            db.execute(
                "INSERT INTO action_oauth_tokens(action_id,owner,kind,token,expires_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(action_id,owner,kind) DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at",
                (action["id"], action["owner"], kind, sealed, expires),
            )
        db.execute(
            "DELETE FROM action_oauth_tokens WHERE action_id=? AND owner=? AND kind=?",
            (action["id"], action["owner"], _REFRESH_KIND),
        )

    def store_code_tokens(
        self, owner: str, action_id: str, version: int, result: dict
    ) -> None:
        """Commit a code exchange against its exact version; callback fences separately."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            action = self._action(db, owner, action_id, version)
            self._store(
                db, action, result, now=int(time.time()), preserve_refresh=False
            )

    async def access_token(
        self, owner: str, action_id: str, version: int
    ) -> str | None:
        """Serialize refreshes across processes and never replay uncertain rotation."""
        claim_id = secrets.token_urlsafe(24)
        deadline = time.monotonic() + _REFRESH_WAIT
        while True:
            with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                action = self._action(db, owner, action_id, version)
                now = int(time.time())
                active = self._row(db, action, _REFRESH_KIND)
                if active is not None and active["expires_at"] <= now:
                    self._clear(db, action)
                    return None
                if active is None:
                    token = self._token(db, action, "access", now)
                    if token is not None:
                        return token
                    refresh = self._token(db, action, "refresh", now)
                    if refresh is None:
                        return None
                    meta = json.loads(action["metadata"])
                    creds = self.repo._credentials(action)
                    db.execute(
                        "INSERT INTO action_oauth_tokens(action_id,owner,kind,token,expires_at) VALUES(?,?,?,?,?)",
                        (
                            action_id,
                            owner,
                            _REFRESH_KIND,
                            claim_id,
                            now + _REFRESH_LEASE,
                        ),
                    )
                    break
            if time.monotonic() >= deadline:
                raise TimeoutError("OAuth Action refresh busy")
            await asyncio.sleep(0.15)
        result = None
        accepted = False
        try:
            auth = meta["auth"]
            result = await exchange(
                auth["client_url"],
                {"grant_type": "refresh_token", "refresh_token": refresh},
                creds,
                auth["token_exchange_method"],
            )
        except (
            ValueError,
            PermissionError,
            ConnectionError,
            TimeoutError,
            OSError,
        ):
            return None
        finally:
            # Cancellation, process loss and a rejected exchange are all
            # unsafe to replay: the remote token may have rotated already.
            with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    action = self._action(db, owner, action_id, version)
                except PermissionError:
                    action = None
                if action is not None:
                    active = self._row(db, action, _REFRESH_KIND)
                    if active is not None and active["token"] == claim_id:
                        if result is not None and active["expires_at"] > int(
                            time.time()
                        ):
                            self._store(
                                db,
                                action,
                                result,
                                now=int(time.time()),
                                preserve_refresh=True,
                            )
                            accepted = True
                        else:
                            self._clear(db, action)
        if not accepted or result is None:
            raise PermissionError("OAuth Action changed during refresh")
        return result["access_token"]
