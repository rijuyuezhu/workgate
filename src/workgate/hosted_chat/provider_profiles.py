"""Controller-owned model channels adapted from final v6.0.3 provider_profiles.py.

Storage and validation only. The Control route/transport must authorize access
and enforce the configured network policy before making provider requests.
"""

import json
import re
import sqlite3
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from .database import SQLiteDatabase
from .network import validate_provider_endpoint
from .sampling import validate_sampling

if TYPE_CHECKING:
    from .credential_cipher import CredentialCipher

_OWNER = "__workgate_controller__"
_PREFIX = "model.provider."
_CHANNEL_PREFIX = "model.channel."
_DEFAULTS = {
    "openai": ("https://api.openai.com/v1", "gpt-4.1-mini"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-6"),
    "google": (
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-2.5-flash",
    ),
}
_PROTOCOLS = frozenset(_DEFAULTS) | {"openai-responses"}
# Built-in provider aliases resolve case-insensitively via profile(). Use the
# same aliases when checking references before resetting saved credentials.
_LEGACY_ALIASES = {
    "openai": (
        "openai",
        "openai-compatible",
        "ollama",
        "azureopenai",
        "custom",
    ),
    "anthropic": ("anthropic",),
    "google": ("google", "gemini"),
}
# LibreChat endpointSchema.name must not collide with EModelEndpoint values.
_RESERVED = frozenset(
    {
        "agents",
        "openai",
        "anthropic",
        "google",
        "gemini",
        "custom",
        "azureopenai",
        "assistants",
        "azureassistants",
        "bedrock",
        "gptplugins",
        "chatgptbrowser",
        "ollama",
        "openai-compatible",
    }
)
# Keep channel IDs path-safe while allowing Unicode letters (including CJK).
# The same rule is used for creation, renaming and deletion.
_NAME = re.compile(r"^[^\W\d_][\w .-]{0,63}$")


def _valid_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(_NAME.fullmatch(name))
        and name == name.strip()
        and name.lower() not in _RESERVED
        and not name.lower().endswith("assistants")
    )


def validate_connection(body: dict) -> tuple[str, str, str | None]:
    kind = body.get("provider")
    if not isinstance(kind, str) or kind not in _PROTOCOLS:
        raise ValueError("Unsupported model provider")
    base = body.get("base_url")
    if not isinstance(base, str) or not base.strip() or len(base) > 2048:
        raise ValueError("A model provider base URL is required")
    base = base.strip().rstrip("/")
    validate_provider_endpoint(
        base, allow_private_http=body.get("allow_private_http", False)
    )
    key = body.get("api_key")
    if key is not None and (
        not isinstance(key, str)
        or len(key) > 16384
        or any(ord(char) < 32 for char in key)
    ):
        raise ValueError("Invalid model API key")
    return kind, base, (key.strip() or None) if key else None


def _validate(body: dict) -> dict:
    kind, base, key = validate_connection(body)
    name = body.get("name")
    if kind == "openai-responses" and name is None:
        raise ValueError("Responses channels require a channel name")
    if name is not None and not _valid_name(name):
        raise ValueError("Invalid or reserved channel name")
    keyless = body.get("keyless", False)
    if type(keyless) is not bool or (
        keyless and (key or kind not in {"openai", "openai-responses"})
    ):
        raise ValueError("Invalid keyless model channel")
    auto_discover = body.get("auto_discover", False)
    if type(auto_discover) is not bool:
        raise ValueError("auto_discover must be a boolean")
    models = body.get("models", [])
    if (
        not isinstance(models, list)
        or (not models and not auto_discover)
        or len(models) > 128
    ):
        raise ValueError("Provide between 1 and 128 model identifiers")
    if any(
        not isinstance(item, str)
        or not item.strip()
        or len(item) > 200
        or any(ord(char) < 32 for char in item)
        for item in models
    ):
        raise ValueError("Invalid model identifier")
    models = list(dict.fromkeys(item.strip() for item in models))
    if kind == "google" and any(
        any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for char in item
        )
        for item in models
    ):
        raise ValueError("Unsupported Gemini model identifier")
    model_contexts = body.get("model_contexts", {})
    if not isinstance(model_contexts, dict) or any(
        model not in models
        or type(limit) is not int
        or not 1 <= limit <= 2_000_000
        for model, limit in model_contexts.items()
    ):
        raise ValueError(
            "Model context windows must be integers from 1 to 2000000 for configured models"
        )
    model_sampling = body.get("model_sampling")
    if model_sampling is not None:
        if not isinstance(model_sampling, dict) or model_sampling.keys() - set(
            models
        ):
            raise ValueError(
                "Sampling defaults must belong to configured models"
            )
        model_sampling = {
            model: validate_sampling(value, provider=kind)
            for model, value in model_sampling.items()
        }
    return {
        **({"name": name} if name is not None else {}),
        "provider": kind,
        "base_url": base,
        "models": models,
        "api_key": key,
        "model_contexts": model_contexts,
        "auto_discover": auto_discover,
        "allow_private_http": body.get("allow_private_http", False),
        "keyless": keyless,
        "model_sampling": model_sampling,
    }


def configured(value: dict) -> bool:
    return bool(
        value["models"]
        and (
            value.get("api_key")
            or value.get("api_key_ciphertext")
            or value.get("keyless")
            or (value.get("oauth_id") and not value.get("oauth_error"))
            or (
                value["provider"] in {"openai", "openai-responses"}
                and urlsplit(value["base_url"]).hostname
                in {"127.0.0.1", "localhost", "::1"}
            )
        )
    )


def _redact(value: dict) -> dict:
    parsed = urlsplit(value["base_url"])
    return {
        "provider": value["provider"],
        **({"name": value["name"]} if "name" in value else {}),
        "base_url": urlunsplit(
            (
                parsed.scheme,
                parsed.netloc.rsplit("@", 1)[-1],
                parsed.path,
                "",
                "",
            )
        ),
        "allow_private_http": value.get("allow_private_http", False),
        "keyless": value.get("keyless", False),
        "models": value["models"],
        "model_contexts": value.get("model_contexts", {}),
        "model_sampling": value.get("model_sampling") or {},
        "auto_discover": value.get("auto_discover") is True,
        "discovered_model_contexts": value.get("discovered_model_contexts")
        or {},
        "model_context_overrides": value.get("model_context_overrides") or {},
        "model_sync_at": value.get("model_sync_at"),
        "model_sync_error": value.get("model_sync_error"),
        "configured": configured(value),
        **(
            {
                "auth_method": value["auth_method"],
                "oauth_error": value.get("oauth_error"),
            }
            if value.get("oauth_id")
            else {}
        ),
    }


def _reference_types(db, name: str, *, legacy: bool = False) -> list[str]:
    # Retain snapshots and chat history; a forced removal must not rewrite the
    # original provider identity to point at unrelated credentials.
    references = (
        ("agents", "json_extract(config,'$.provider')", "Agents"),
        ("runs", "json_extract(state,'$.provider')", "run history"),
        ("conversations", "endpoint", "saved conversations"),
        (
            "agent_versions",
            "json_extract(snapshot,'$.provider')",
            "Agent revisions",
        ),
    )
    aliases = _LEGACY_ALIASES[name] if legacy else ()
    used_by = []
    for table, field, label in references:
        if legacy:
            placeholders = ",".join("?" for _ in aliases)
            query = f"SELECT 1 FROM {table} WHERE LOWER(TRIM({field})) IN ({placeholders}) LIMIT 1"
            params = aliases
        else:
            query = f"SELECT 1 FROM {table} WHERE {field}=? LIMIT 1"
            params = (name,)
        if db.execute(query, params).fetchone():
            used_by.append(label)
    return used_by


def _is_referenced(db, name: str, *, legacy: bool = False) -> bool:
    return bool(_reference_types(db, name, legacy=legacy))


class ChannelConflict(ValueError):
    def __init__(self, message: str, *, can_force: bool = False) -> None:
        super().__init__(message)
        self.can_force = can_force


_SYNC_ERROR = (
    "Unable to refresh model information. Last successful data is retained."
)


def _apply_catalog(value: dict, catalog: dict, overrides: dict) -> None:
    models = catalog["models"]
    reported = {
        m: n for m, n in catalog["model_contexts"].items() if m in models
    }
    overrides = {m: n for m, n in overrides.items() if m in models}
    value.update(
        models=models,
        discovered_model_contexts=reported,
        model_context_overrides=overrides,
        model_contexts={**reported, **overrides},
        model_sync_at=time.time(),
        model_sync_error=None,
    )
    # Only active models need defaults in the editable catalog. Historical
    # conversations and Agent references are not rewritten.
    value["model_sampling"] = {
        m: v
        for m, v in (value.get("model_sampling") or {}).items()
        if m in models
    }


def apply_auto_save(value: dict, previous: dict, catalog: dict | None) -> None:
    overrides = value["model_contexts"]
    if catalog is not None:
        _apply_catalog(value, catalog, overrides)
        return
    if previous.get("auto_discover") is True:
        reported = previous.get("discovered_model_contexts") or {}
    else:
        # An old/manual list is a valid fallback, not discovered metadata.
        reported = {}
        overrides = {**(previous.get("model_contexts") or {}), **overrides}
    _apply_catalog(
        value,
        {"models": previous["models"], "model_contexts": reported},
        overrides,
    )
    value["model_sync_at"] = previous.get("model_sync_at")
    value["model_sync_error"] = _SYNC_ERROR


class ProviderRepository:
    def __init__(
        self, database: SQLiteDatabase, cipher: CredentialCipher
    ) -> None:
        self.database = database
        self.cipher = cipher

    @staticmethod
    def _default_profile(provider: str) -> dict:
        base, model = _DEFAULTS[provider]
        return {"provider": provider, "base_url": base, "models": [model]}

    @staticmethod
    def _read(db: sqlite3.Connection, key: str) -> dict | None:
        row = db.execute(
            "SELECT value FROM settings WHERE owner=? AND key=?", (_OWNER, key)
        ).fetchone()
        return json.loads(row[0]) if row is not None else None

    @staticmethod
    def _stored_channels(db: sqlite3.Connection) -> list[dict]:
        rows = db.execute(
            "SELECT value FROM settings WHERE owner=? AND key LIKE ? ORDER BY key",
            (_OWNER, _CHANNEL_PREFIX + "%"),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _binding(value: dict) -> str:
        # JSON avoids delimiter ambiguities in channel names and endpoints.
        return json.dumps(
            [
                "model-channel",
                value.get("name", value["provider"]),
                value["provider"],
                value["base_url"],
            ],
            ensure_ascii=False,
        )

    def _secret(self, value: dict) -> str:
        token = value.get("api_key_ciphertext")
        if token is None:
            return ""
        return self.cipher.unseal(token, binding=self._binding(value))[
            "api_key"
        ]

    @classmethod
    def _profile_record(cls, db: sqlite3.Connection, channel: str) -> dict:
        """Resolve the channel in the caller's snapshot without decrypting keys."""
        if not isinstance(channel, str):
            raise ValueError("Invalid model channel")
        normalized = channel.strip().lower()
        for kind, aliases in _LEGACY_ALIASES.items():
            if normalized in aliases:
                normalized = kind
                break
        if normalized in _DEFAULTS:
            value = cls._read(db, _PREFIX + normalized) or cls._default_profile(
                normalized
            )
        else:
            value = cls._read(db, _CHANNEL_PREFIX + channel)
            if not value or value.get("name") != channel:
                raise ValueError("Unknown model channel")
        return value

    def profile(self, channel: str) -> dict:
        """Internal inference profile; never return this object from a public route."""
        with self.database.read_snapshot() as db:
            value = self._profile_record(db, channel)
        return {
            **{k: v for k, v in value.items() if k != "api_key_ciphertext"},
            "api_key": self._secret(value),
        }

    def public_profiles(self) -> dict:
        with self.database.read_snapshot() as db:
            entries = []
            for kind in _DEFAULTS:
                saved = self._read(db, _PREFIX + kind)
                entries.append(
                    {
                        **_redact(saved or self._default_profile(kind)),
                        "source": "saved" if saved is not None else "default",
                    }
                )
            entries.extend(
                {**_redact(value), "source": "named"}
                for value in self._stored_channels(db)
            )
        return {"providers": entries}

    @staticmethod
    def _save_keys(body: dict, value: dict) -> tuple[str | None, str, str]:
        old_name = body.get("previous_name")
        name = value.get("name")
        if old_name is not None and (not _valid_name(old_name) or name is None):
            raise ValueError("Invalid previous channel name")
        assert old_name is None or isinstance(old_name, str)
        renaming = old_name is not None and old_name != name
        key = (
            _CHANNEL_PREFIX + name
            if name is not None
            else _PREFIX + value["provider"]
        )
        previous_key = (
            _CHANNEL_PREFIX + old_name
            if old_name is not None and renaming
            else key
        )
        return old_name, key, previous_key

    def save(self, body: dict) -> dict:
        value = _validate(body)
        if value["auto_discover"]:
            raise ValueError(
                "Automatic discovery requires the model discovery service"
            )
        return self._save_validated(body, value)

    def _save_validated(
        self,
        body: dict,
        value: dict,
        *,
        prepared: tuple[str | None, dict | None] | None = None,
    ) -> dict:
        old_name, key, previous_key = self._save_keys(body, value)
        name = value.get("name")
        renaming = old_name is not None and old_name != name
        supplied = value.pop("api_key")
        if supplied:
            # Initialize outside the profile transaction: the key identity
            # itself is committed before any encrypted record can be saved.
            self.cipher.initialize()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM settings WHERE owner=? AND key=?",
                (_OWNER, previous_key),
            ).fetchone()
            raw = row[0] if row is not None else None
            if prepared is not None and raw != prepared[0]:
                raise ChannelConflict(
                    "Channel changed during discovery; retry saving"
                )
            previous = json.loads(raw) if raw is not None else None
            if name is not None:
                existing = self._stored_channels(db)
                if any(
                    ch["name"].lower() == name.lower()
                    and ch["name"] not in {name, old_name}
                    for ch in existing
                ):
                    raise ChannelConflict("Channel name already exists")
                if len(existing) >= 64 and not (
                    renaming or previous is not None
                ):
                    raise ValueError("At most 64 named channels are supported")
                if (renaming or previous is None) and _is_referenced(db, name):
                    raise ChannelConflict(
                        "Channel name is retained by saved history; select another name"
                    )
            if old_name is not None and renaming:
                if previous is None:
                    raise KeyError("Original channel no longer exists")
                if self._read(db, key) is not None:
                    raise ChannelConflict("Channel name already exists")
                if _is_referenced(db, old_name):
                    raise ChannelConflict(
                        "Channel is referenced by saved conversations or Agents"
                    )
            previous = previous or {}
            if previous and previous.get("provider") != value["provider"]:
                raise ChannelConflict(
                    "Existing channel provider type cannot be changed"
                )
            if previous.get("oauth_id"):
                raise ChannelConflict(
                    "Account channels require the account authorization service"
                )
            if "keyless" not in body and supplied is None:
                value["keyless"] = previous.get("keyless", False)
            if value["keyless"]:
                secret = ""
            elif supplied is not None:
                secret = supplied
            elif previous.get("api_key_ciphertext"):
                if previous["base_url"] != value["base_url"]:
                    raise ValueError(
                        "Enter an API key or explicitly choose keyless at a changed endpoint"
                    )
                secret = self._secret(previous)
            else:
                secret = ""
            if secret:
                if supplied is None and not renaming:
                    value["api_key_ciphertext"] = previous["api_key_ciphertext"]
                else:
                    value["api_key_ciphertext"] = self.cipher.seal(
                        {"api_key": secret}, binding=self._binding(value)
                    )
            if value["model_sampling"] is None:
                value["model_sampling"] = {
                    model: sampling
                    for model, sampling in (
                        previous.get("model_sampling") or {}
                    ).items()
                    if value["auto_discover"] or model in value["models"]
                }
            if prepared is not None:
                apply_auto_save(value, previous, prepared[1])
            db.execute(
                "INSERT INTO settings(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value",
                (_OWNER, key, json.dumps(value)),
            )
            if renaming:
                db.execute(
                    "DELETE FROM settings WHERE owner=? AND key=?",
                    (_OWNER, previous_key),
                )
        return self.public_profiles()

    def delete(
        self,
        *,
        name: str | None = None,
        legacy_provider: str | None = None,
        force: bool = False,
    ) -> dict:
        if (name is None) == (legacy_provider is None) or type(
            force
        ) is not bool:
            raise ValueError("Specify a named or saved legacy channel")
        if legacy_provider is not None:
            if legacy_provider not in _DEFAULTS:
                raise ValueError("Invalid legacy channel")
            key = _PREFIX + legacy_provider
            target = legacy_provider
        else:
            if not isinstance(name, str) or not _valid_name(name):
                raise ValueError("Invalid named channel")
            key = _CHANNEL_PREFIX + name
            target = name
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            references = _reference_types(
                db, target, legacy=legacy_provider is not None
            )
            if references and (not force or legacy_provider is not None):
                raise ChannelConflict(
                    "Channel is referenced by " + ", ".join(references),
                    can_force=legacy_provider is None,
                )
            current = self._read(db, key)
            if current and current.get("oauth_id"):
                db.execute(
                    "DELETE FROM settings WHERE owner=? AND key=?",
                    (_OWNER, "model.oauth.refresh." + current["oauth_id"]),
                )
            db.execute(
                "DELETE FROM settings WHERE owner=? AND key=?", (_OWNER, key)
            )
        return self.public_profiles()
