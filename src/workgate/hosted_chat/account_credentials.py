"""Account credential persistence and refresh, from final upstream v6.0.3.

Source: f0ecf8f8039345f24d0de4402983766787f4574e,
webui/model_oauth.py (fab8ab54c6e84a9d1caaeefa75bb6c16d622b00f).
See LICENSE.upstream. No runtime activation or route registration.
"""

import asyncio
import json
import secrets
import time
import uuid

import httpx

from .credential_cipher import CredentialUnavailable
from .model_oauth import ReauthorizationRequired, adapter
from .provider_profiles import (
    _CHANNEL_PREFIX, _OWNER, _apply_catalog, _is_referenced, _validate,
    ChannelConflict, ProviderRepository,
)


def _cipher(database):
    from .actions import ActionRepository
    return ActionRepository(database)._cipher()


def seal(database, value: dict) -> str:
    return _cipher(database).encrypt(json.dumps(value).encode()).decode()


def unseal(database, value: dict) -> dict:
    from cryptography.fernet import InvalidToken
    try:
        return json.loads(_cipher(database).decrypt(value["oauth_credentials"].encode()))
    except (KeyError, ValueError, InvalidToken, TypeError):
        raise ReauthorizationRequired("Account credentials are unavailable; sign in again") from None


def check_binding(value: dict):
    auth = adapter(value["auth_method"])
    if (value["provider"], value["base_url"]) != (auth.protocol, auth.base_url):
        raise ValueError("Account credentials are bound to the original API endpoint")
    return auth


def host_id(database) -> str:
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM settings WHERE owner=? AND key='model.oauth.host'", (_OWNER,)).fetchone()
        if row:
            return json.loads(row[0])
        value = "urn:uuid:" + str(uuid.uuid4())
        db.execute("INSERT INTO settings(owner,key,value) VALUES(?,?,?)",
                   (_OWNER, "model.oauth.host", json.dumps(value)))
        return value


async def access_token(database, name: str, identity: str, *, rejected: str | None = None) -> str:
    """Serialize rotating refreshes across Controller processes via SQLite lease."""
    lease = "model.oauth.refresh." + identity
    lock_id = secrets.token_hex(16)
    until = time.monotonic() + 45
    while True:
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT key,value FROM settings WHERE owner=? AND key LIKE ? AND json_extract(value,'$.oauth_id')=?",
                             (_OWNER, _CHANNEL_PREFIX + "%", identity)).fetchone()
            value = json.loads(row["value"]) if row else {}
            if value.get("oauth_id") != identity or value.get("oauth_error"):
                raise ReauthorizationRequired("Account authorization changed or expired; sign in again")
            auth = check_binding(value)
            credentials = unseal(database, value)
            token = credentials["access_token"]
            now = time.time()
            if credentials["expires_at"] > now + 120 and token != rejected:
                return token
            if credentials.get("earliest_refresh_at", 0) > now:
                if token != rejected and credentials["expires_at"] > now:
                    return token
                raise ValueError("Account credential cannot be refreshed yet; retry later")
            row = db.execute("SELECT value FROM settings WHERE owner=? AND key=?", (_OWNER, lease)).fetchone()
            if not row or json.loads(row[0])["until"] < time.time():
                db.execute("INSERT INTO settings(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) "
                           "DO UPDATE SET value=excluded.value",
                           (_OWNER, lease, json.dumps({"owner": lock_id, "until": time.time() + 90})))
                break
        if time.monotonic() >= until:
            raise ValueError("Account credential refresh is busy; retry")
        await asyncio.sleep(.15)
    try:
        error = None
        try:
            updated = await auth.refresh(credentials)
            encrypted = seal(database, updated)
        except ReauthorizationRequired as exc:
            error = str(exc)
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT key,value FROM settings WHERE owner=? AND key LIKE ? AND json_extract(value,'$.oauth_id')=?",
                             (_OWNER, _CHANNEL_PREFIX + "%", identity)).fetchone()
            current = json.loads(row["value"]) if row else {}
            if current.get("oauth_id") != identity or current.get("oauth_credentials") != value["oauth_credentials"]:
                raise ValueError("Account channel changed during credential refresh")
            if error:
                current["oauth_error"] = error
            else:
                current["oauth_credentials"] = encrypted
            db.execute("UPDATE settings SET value=? WHERE owner=? AND key=?", (json.dumps(current), _OWNER, row["key"]))
        if error:
            raise ReauthorizationRequired(error)
        return updated["access_token"]
    finally:
        with database.connect() as db:
            db.execute("DELETE FROM settings WHERE owner=? AND key=? AND json_extract(value,'$.owner')=?",
                       (_OWNER, lease, lock_id))


async def catalog_for(database, value: dict, *, rejected: str | None = None) -> dict:
    auth = check_binding(value)
    token = await access_token(database, value["name"], value["oauth_id"], rejected=rejected)
    try:
        return await auth.catalog(token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
    token = await access_token(database, value["name"], value["oauth_id"], rejected=token)
    return await auth.catalog(token)


async def save_account(database, method: str, name: str, credentials: dict, *, original: dict | None = None):
    from .model_discovery import _apply_catalog
    auth = adapter(method)
    if not isinstance(name, str) or not name:
        raise ValueError("Channel name is required")
    value = _validate({"name": name, "provider": auth.protocol, "base_url": auth.base_url,
                       "models": [], "auto_discover": True})
    catalog = await auth.catalog(credentials["access_token"])
    if not catalog["models"]:
        raise ValueError("No eligible models available for this account")
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT value FROM settings WHERE owner=? AND key LIKE ?",
                          (_OWNER, _CHANNEL_PREFIX + "%")).fetchall()
        existing = [json.loads(row[0]) for row in rows]
        current = next((v for v in existing if v["name"].lower() == name.lower()), None)
        if original:
            if not current or current.get("oauth_id") != original.get("oauth_id") or current["name"] != name:
                raise ValueError("Account channel changed during authorization")
            prior = unseal(database, current)
            if prior["client_id"] != credentials["client_id"] or prior["subject"] != credentials["subject"]:
                raise ValueError("Reauthorization must use the original account")
            value = current.copy()
        elif current or len(existing) >= 64:
            raise ValueError("Channel name already exists or channel limit reached")
        # Prevent duplicate rotating sessions for the same issued registration.
        for other in existing:
            if (other.get("auth_method") == method and other["name"] != name
                    and unseal(database, other)["client_id"] == credentials["client_id"]):
                raise ValueError("This account registration is already connected")
        value.update(auth_method=method, oauth_id=uuid.uuid4().hex,
                     oauth_credentials=seal(database, credentials), oauth_error=None, api_key="")
        if value["auto_discover"]:
            _apply_catalog(value, catalog, value.get("model_context_overrides") or {})
        db.execute("INSERT INTO settings(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) "
                   "DO UPDATE SET value=excluded.value", (_OWNER, _CHANNEL_PREFIX + name, json.dumps(value)))


