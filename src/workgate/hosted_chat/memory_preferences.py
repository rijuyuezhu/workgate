"""Owner-controlled, validated opt-in settings for the separate Memory Agent."""

import json
from collections.abc import Callable

from .credential_cipher import CredentialCipher
from .database import SQLiteDatabase
from .memory import _memory_config_snapshot
from .provider_profiles import ProviderRepository
from .providers import provider_config


def _public(config: dict | None) -> dict:
    value = (config or {}).get("agent")
    selected = value if isinstance(value, dict) else {}
    return {
        "available": config is not None,
        "enabled": config is not None and selected.get("enabled") is True,
        **{
            key: selected[key]
            for key in ("id", "provider", "model")
            if isinstance(selected.get(key), str)
        },
        "messageWindowSize": (config or {}).get("messageWindowSize", 5),
        "maxInputTokens": (config or {}).get("maxInputTokens", 4096),
    }


def automatic_memory_preferences(
    database: SQLiteDatabase,
    owner: str,
    *,
    changes: dict | None = None,
    check_grant: Callable[[], None],
) -> dict:
    check_grant()
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE" if changes is not None else "BEGIN")
        config = _memory_config_snapshot(db, owner)
        if changes is None:
            return _public(config)
        if config is None:
            raise PermissionError("Memory is disabled")
        if (
            not isinstance(changes, dict)
            or not set(changes)
            <= {
                "enabled",
                "id",
                "provider",
                "model",
                "messageWindowSize",
                "maxInputTokens",
            }
            or type(changes.get("enabled")) is not bool
        ):
            raise ValueError("Invalid automatic memory preference")
        enabled = changes["enabled"]
        window = changes.get(
            "messageWindowSize", config.get("messageWindowSize", 5)
        )
        tokens = changes.get(
            "maxInputTokens", config.get("maxInputTokens", 4096)
        )
        if (
            type(window) is not int
            or not 1 <= window <= 100
            or type(tokens) is not int
            or not 32 <= tokens <= 32768
        ):
            raise ValueError("Invalid automatic memory window")
        selection = {
            key: changes[key]
            for key in ("id", "provider", "model")
            if key in changes
        }
        if any(
            not isinstance(value, str) or not value or len(value) > 200
            for value in selection.values()
        ):
            raise ValueError("Invalid automatic memory model selection")
        if enabled:
            if set(selection) == {"id"}:
                row = db.execute(
                    "SELECT config FROM agents WHERE id=? AND owner=?",
                    (selection["id"], owner),
                ).fetchone()
                if row is None:
                    raise KeyError("Memory Agent not found")
                agent = json.loads(row["config"])
                provider, model = agent.get("provider"), agent.get("model")
            elif set(selection) == {"provider", "model"}:
                provider, model = selection["provider"], selection["model"]
            else:
                raise ValueError("Select one automatic memory Agent or model")
            if not isinstance(provider, str) or not isinstance(model, str):
                raise ValueError("Invalid automatic memory model")
            provider_config(
                ProviderRepository(database, CredentialCipher(database)),
                provider,
                model,
                allow_account=False,
            )
        elif selection:
            raise ValueError("Disable without a model selection")
        config = {
            **config,
            "agent": {"enabled": enabled, **selection},
            "messageWindowSize": window,
            "maxInputTokens": tokens,
        }
        check_grant()
        db.execute(
            "INSERT INTO settings(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value",
            (
                owner,
                "webui.memory.config",
                json.dumps(config, separators=(",", ":")),
            ),
        )
        return _public(config)
