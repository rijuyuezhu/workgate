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
    _CHANNEL_PREFIX,
    _OWNER,
    ChannelConflict,
    ProviderRepository,
    _apply_catalog,
    _is_referenced,
    _validate,
)

_REFRESH_WAIT_S = 45
_REFRESH_LEASE_S = 90
_REFRESH_TIMEOUT_S = 40
_REAUTHORIZE = "Account credential refresh was interrupted; sign in again"


def check_binding(value: dict):
    auth = adapter(value.get("auth_method"))
    if (value.get("provider"), value.get("base_url")) != (
        auth.protocol,
        auth.base_url,
    ):
        raise ValueError(
            "Account credentials are bound to the original API endpoint"
        )
    if not isinstance(value.get("oauth_id"), str) or not value["oauth_id"]:
        raise ReauthorizationRequired("Invalid account channel identity")
    return auth


def _binding(value: dict) -> str:
    check_binding(value)
    return json.dumps(
        [
            "model-account",
            value["oauth_id"],
            value["auth_method"],
            value["provider"],
            value["base_url"],
        ]
    )


def seal(repo: ProviderRepository, value: dict, credentials: dict) -> str:
    return repo.cipher.seal(credentials, binding=_binding(value))


def unseal(repo: ProviderRepository, value: dict) -> dict:
    try:
        return repo.cipher.unseal(
            value["oauth_credentials"], binding=_binding(value)
        )
    except KeyError, CredentialUnavailable, ValueError, TypeError:
        raise ReauthorizationRequired(
            "Account credentials are unavailable; sign in again"
        ) from None


def host_id(repo: ProviderRepository) -> str:
    with repo.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT value FROM settings WHERE owner=? AND key='model.oauth.host'",
            (_OWNER,),
        ).fetchone()
        if row:
            return json.loads(row[0])
        value = "urn:uuid:" + str(uuid.uuid4())
        db.execute(
            "INSERT INTO settings(owner,key,value) VALUES(?,?,?)",
            (_OWNER, "model.oauth.host", json.dumps(value)),
        )
        return value


def _account_row(db, identity: str):
    return db.execute(
        "SELECT key,value FROM settings WHERE owner=? AND key LIKE ? AND json_extract(value,'$.oauth_id')=?",
        (_OWNER, _CHANNEL_PREFIX + "%", identity),
    ).fetchone()


def _lease_row(db, lease: str) -> dict | None:
    row = db.execute(
        "SELECT value FROM settings WHERE owner=? AND key=?", (_OWNER, lease)
    ).fetchone()
    return json.loads(row[0]) if row is not None else None


def _store_value(db, key: str, value: dict) -> None:
    db.execute(
        "INSERT INTO settings(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value",
        (_OWNER, key, json.dumps(value)),
    )


async def access_token(
    repo: ProviderRepository,
    name: str,
    identity: str,
    *,
    rejected: str | None = None,
) -> str:
    """Serialize rotating refreshes with a durable, fenced SQLite lease.

    A lease left by a crashed process cannot safely replay a rotating refresh.
    Once it expires, require explicit account reauthorization instead.
    """
    if (
        not isinstance(identity, str)
        or not identity
        or not isinstance(name, str)
    ):
        raise ReauthorizationRequired("Invalid account channel identity")
    lease = "model.oauth.refresh." + identity
    lock_id = secrets.token_hex(16)
    deadline = time.monotonic() + _REFRESH_WAIT_S
    while True:
        stale = False
        with repo.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = _account_row(db, identity)
            value = json.loads(row["value"]) if row else {}
            if value.get("oauth_id") != identity or value.get("oauth_error"):
                raise ReauthorizationRequired(
                    "Account authorization changed or expired; sign in again"
                )
            auth = check_binding(value)
            credentials = unseal(repo, value)
            token = credentials["access_token"]
            now = time.time()
            active = _lease_row(db, lease)
            if active is not None and active["until"] <= now:
                value["oauth_error"] = _REAUTHORIZE
                _store_value(db, row["key"], value)
                db.execute(
                    "DELETE FROM settings WHERE owner=? AND key=?",
                    (_OWNER, lease),
                )
                stale = True
            elif active is None:
                if credentials["expires_at"] > now + 120 and token != rejected:
                    return token
                if credentials.get("earliest_refresh_at", 0) > now:
                    if token != rejected and credentials["expires_at"] > now:
                        return token
                    raise ValueError(
                        "Account credential cannot be refreshed yet; retry later"
                    )
                _store_value(
                    db,
                    lease,
                    {"owner": lock_id, "until": now + _REFRESH_LEASE_S},
                )
                break
        if stale:
            raise ReauthorizationRequired(_REAUTHORIZE)
        if time.monotonic() >= deadline:
            raise ValueError("Account credential refresh is busy; retry")
        await asyncio.sleep(0.15)

    updated = None
    encrypted = None
    error = None
    try:
        try:
            async with asyncio.timeout(_REFRESH_TIMEOUT_S):
                updated = await auth.refresh(credentials)
            encrypted = seal(repo, value, updated)
        except BaseException as exc:
            # Rotation may already have happened even if its reply was lost.
            # Preserve the last encrypted record and require reauthorization.
            error = _REAUTHORIZE
            if isinstance(exc, ReauthorizationRequired):
                error = "Account authorization expired; sign in again"
            raise
        finally:
            with repo.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = _account_row(db, identity)
                current = json.loads(row["value"]) if row else {}
                active = _lease_row(db, lease)
                owns_lease = active is not None and active["owner"] == lock_id
                unchanged = (
                    current.get("oauth_id") == identity
                    and current.get("oauth_credentials")
                    == value["oauth_credentials"]
                )
                if owns_lease and unchanged:
                    if error is not None or active["until"] <= time.time():
                        current["oauth_error"] = error or _REAUTHORIZE
                    else:
                        current["oauth_credentials"] = encrypted
                    _store_value(db, row["key"], current)
                if owns_lease:
                    db.execute(
                        "DELETE FROM settings WHERE owner=? AND key=?",
                        (_OWNER, lease),
                    )
                accepted = (
                    owns_lease and unchanged and not current.get("oauth_error")
                )
        if not accepted or updated is None:
            raise ReauthorizationRequired(
                "Account channel changed during credential refresh"
            )
        return updated["access_token"]
    except httpx.HTTPError, TimeoutError:
        raise ReauthorizationRequired(_REAUTHORIZE) from None


async def catalog_for(
    repo: ProviderRepository, value: dict, *, rejected: str | None = None
) -> dict:
    auth = check_binding(value)
    token = await access_token(
        repo, value["name"], value["oauth_id"], rejected=rejected
    )
    try:
        return await auth.catalog(token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
    token = await access_token(
        repo, value["name"], value["oauth_id"], rejected=token
    )
    return await auth.catalog(token)


async def save_account(
    repo: ProviderRepository,
    method: str,
    name: str,
    credentials: dict,
    *,
    original: dict | None = None,
):
    auth = adapter(method)
    if not isinstance(name, str) or not name:
        raise ValueError("Channel name is required")
    value = _validate(
        {
            "name": name,
            "provider": auth.protocol,
            "base_url": auth.base_url,
            "models": [],
            "auto_discover": True,
        }
    )
    catalog = await auth.catalog(credentials["access_token"])
    if not catalog["models"]:
        raise ValueError("No eligible models available for this account")
    repo.cipher.initialize()
    with repo.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = repo._stored_channels(db)
        current = next(
            (v for v in existing if v["name"].lower() == name.lower()), None
        )
        if original:
            if (
                not current
                or current.get("oauth_id") != original.get("oauth_id")
                or current["name"] != name
                or current.get("oauth_credentials")
                != original.get("oauth_credentials")
            ):
                raise ChannelConflict(
                    "Account channel changed during authorization"
                )
            prior = unseal(repo, current)
            if (
                prior["client_id"] != credentials["client_id"]
                or prior["subject"] != credentials["subject"]
            ):
                raise ValueError(
                    "Reauthorization must use the original account"
                )
            value = current.copy()
        elif current or len(existing) >= 64:
            raise ChannelConflict(
                "Channel name already exists or channel limit reached"
            )
        elif _is_referenced(db, name):
            raise ChannelConflict(
                "Channel name is retained by saved history; select another name"
            )
        for other in existing:
            if (
                other.get("auth_method") == method
                and other["name"] != name
                and unseal(repo, other)["client_id"] == credentials["client_id"]
            ):
                raise ChannelConflict(
                    "This account registration is already connected"
                )
        value.update(
            auth_method=method, oauth_id=uuid.uuid4().hex, oauth_error=None
        )
        value.pop("api_key", None)
        value["oauth_credentials"] = seal(repo, value, credentials)
        if value["auto_discover"]:
            _apply_catalog(
                value, catalog, value.get("model_context_overrides") or {}
            )
        _store_value(db, _CHANNEL_PREFIX + name, value)
        if original:
            db.execute(
                "DELETE FROM settings WHERE owner=? AND key=?",
                (_OWNER, "model.oauth.refresh." + original["oauth_id"]),
            )
    return repo.public_profiles()


def update_account(repo: ProviderRepository, body: dict) -> dict:
    """Apply ordinary channel settings without allowing credentials to move endpoints."""
    value = _validate(body)
    old_name, key, previous_key = repo._save_keys(body, value)
    name = value.get("name")
    if not isinstance(name, str):
        raise ValueError("Channel name is required")
    with repo.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = repo._read(db, previous_key)
        if not previous or not previous.get("oauth_id"):
            raise ChannelConflict("Unknown account channel")
        auth = check_binding(previous)
        if (value["provider"], value["base_url"]) != (
            auth.protocol,
            auth.base_url,
        ):
            raise ValueError(
                "Account credentials are bound to the original API endpoint"
            )
        if (
            value.get("api_key")
            or value.get("keyless")
            or value.get("allow_private_http")
        ):
            raise ValueError(
                "Account channels cannot use API-key or private HTTP settings"
            )
        if old_name is not None and name != old_name:
            if _is_referenced(db, old_name) or _is_referenced(db, name):
                raise ChannelConflict(
                    "Channel is referenced by saved conversations or Agents"
                )
            if any(
                ch["name"].lower() == name.lower() and ch["name"] != old_name
                for ch in repo._stored_channels(db)
            ):
                raise ChannelConflict("Channel name already exists")
        value.pop("api_key", None)
        for field in (
            "oauth_id",
            "auth_method",
            "oauth_credentials",
            "oauth_error",
        ):
            value[field] = previous.get(field)
        if value["model_sampling"] is None:
            value["model_sampling"] = {
                m: v
                for m, v in (previous.get("model_sampling") or {}).items()
                if m in value["models"]
            }
        if value["auto_discover"]:
            overrides = value["model_contexts"]
            _apply_catalog(
                value,
                {
                    "models": previous["models"],
                    "model_contexts": previous.get("discovered_model_contexts")
                    or {},
                },
                overrides,
            )
            value["model_sync_at"] = previous.get("model_sync_at")
            value["model_sync_error"] = previous.get("model_sync_error")
        _store_value(db, key, value)
        if key != previous_key:
            db.execute(
                "DELETE FROM settings WHERE owner=? AND key=?",
                (_OWNER, previous_key),
            )
    return repo.public_profiles()
