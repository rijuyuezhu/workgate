"""Final v6.0.3 catalog discovery, adapted to encrypted Workgate profiles.

Source: f0ecf8f8039345f24d0de4402983766787f4574e, upstream
webui/model_discovery.py blob 145ea6abf8113cee559ac71ca5a9ffe9c80b3701.
No refresh loop or network request is started by importing this module.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable

import httpx

from .network import provider_client
from .provider_profiles import (
    _CHANNEL_PREFIX,
    _DEFAULTS,
    _OWNER,
    _PREFIX,
    _SYNC_ERROR,
    ChannelConflict,
    ProviderRepository,
    _apply_catalog,
    _validate,
    validate_connection,
)

logger = logging.getLogger(__name__)

_MAX_MODELS = 128
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _chat_compatible(model: str, item: dict, kind: str = "openai") -> bool:
    # Some OpenAI-compatible catalogs (e.g. OpenRouter) include explicit
    # output modalities or supported endpoints. Prefer those to ID heuristics.
    architecture = item.get("architecture")
    modalities = (
        architecture.get("output_modalities")
        if isinstance(architecture, dict)
        else None
    )
    if isinstance(modalities, list) and modalities and "text" not in modalities:
        return False
    endpoints = item.get("supported_endpoints")
    if isinstance(endpoints, list) and endpoints:
        suffix = (
            "responses" if kind == "openai-responses" else "chat/completions"
        )
        return any(
            isinstance(e, str) and e.rstrip("/").endswith(suffix)
            for e in endpoints
        )
    # Local inference servers can describe non-generative models by task
    # rather than publishing supported_endpoints.
    non_chat = {
        "embed",
        "embedding",
        "embeddings",
        "rerank",
        "reranking",
        "score",
        "classify",
        "classification",
        "reward",
        "pooling",
        "transcription",
        "speech",
        "text-to-speech",
        "image-generation",
        "moderation",
    }
    for key in ("task", "model_type", "type"):
        value = item.get(key)
        if (
            isinstance(value, str)
            and value.strip().lower().replace("_", "-") in non_chat
        ):
            return False
    tasks = item.get("supported_tasks")
    if isinstance(tasks, list) and tasks:
        normalized = {
            t.strip().lower().replace("_", "-")
            for t in tasks
            if isinstance(t, str)
        }
        if normalized and normalized <= non_chat:
            return False
    # Older catalogs often expose only IDs, including non-chat OpenAI models.
    leaf = model.lower().rsplit("/", 1)[-1]
    if re.fullmatch(
        r"(?:babbage|davinci)(?:-\d+)?|gpt-3\.5-turbo-instruct(?:-\d+)?", leaf
    ):
        return False
    return not re.search(
        r"(?:^|[-_.])(?:embeddings?|rerank(?:er)?|whisper|tts|transcri(?:be|ption)|"
        r"dall-e|image|moderation|realtime|text-search|code-search|sora)(?:$|[-_.0-9])",
        leaf,
    )


def _parse_page(kind: str, data: dict, models: dict[str, int | None]) -> dict:
    items = data.get("models" if kind == "google" else "data")
    if not isinstance(items, list):
        raise ValueError("Invalid provider model list")
    for item in items:
        if not isinstance(item, dict):
            continue
        model = item.get("name" if kind == "google" else "id")
        if not isinstance(model, str):
            continue
        if kind == "google":
            methods = item.get("supportedGenerationMethods")
            if (
                not isinstance(methods, list)
                or "generateContent" not in methods
            ):
                continue
            model = model.removeprefix("models/")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
                continue
        if (
            not model.strip()
            or model != model.strip()
            or len(model) > 200
            or any(ord(c) < 32 for c in model)
        ):
            continue
        if kind in {"openai", "openai-responses"} and not _chat_compatible(
            model, item, kind
        ):
            continue
        fields = (
            ("inputTokenLimit",)
            if kind == "google"
            else (
                "context_window",
                "context_length",
                "max_model_len",
                "max_context_tokens",
                "max_input_tokens",
            )
        )
        limit = next(
            (
                item[key]
                for key in fields
                if type(item.get(key)) is int and 0 < item[key] <= 2_000_000
            ),
            None,
        )
        if model not in models or limit is not None:
            models[model] = limit
        if len(models) > _MAX_MODELS:
            raise ValueError(
                "Provider lists more than 128 models; enter the desired models manually"
            )
    if kind == "google" and data.get("nextPageToken"):
        cursor = data["nextPageToken"]
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise ValueError("Invalid model list cursor")
        return {"pageToken": cursor}
    if kind == "anthropic" and data.get("has_more"):
        cursor = data.get("last_id")
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ValueError("Invalid model list cursor")
        return {"after_id": cursor}
    return {}


async def _fetch_models(
    kind: str,
    base: str,
    token: str,
    *,
    allow_empty: bool = False,
    account: bool = False,
    allow_private_http: bool = False,
) -> dict:
    headers = {"accept": "application/json"}
    if token:
        if kind == "google":
            headers["x-goog-api-key"] = token
        elif kind == "anthropic":
            headers["x-api-key"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
    if kind == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    models: dict[str, int | None] = {}
    params: dict = {}
    seen: set[tuple] = set()
    # Do not follow redirects, expose upstream errors, or send saved keys to an
    # edited URL. Bound the entire discovery, including paginated responses.
    async with (
        asyncio.timeout(15),
        provider_client(
            base, allow_private_http=allow_private_http, timeout=10
        ) as client,
    ):
        for _ in range(16):
            async with client.stream(
                "GET", base + "/models", headers=headers, params=params
            ) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ValueError("Provider model list is too large")
                    content.extend(chunk)
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("Invalid provider model list")
            if account:
                if not isinstance(data.get("models"), list):
                    raise ValueError("Invalid account model list")
                data = {
                    "data": [
                        {**item, "id": item.get("slug")}
                        for item in data["models"]
                        if isinstance(item, dict)
                        and item.get("visibility") == "list"
                    ]
                }
            params = _parse_page(kind, data, models)
            if not params:
                break
            cursor = tuple(params.items())
            if cursor in seen:
                raise ValueError("Provider repeated a model list page")
            seen.add(cursor)
        else:
            raise ValueError("Provider model list has too many pages")
    if not models and not allow_empty:
        raise ValueError(
            "Provider returned no supported models; enter model identifiers manually"
        )
    return {
        "models": list(models),
        "model_contexts": {m: n for m, n in models.items() if n is not None},
    }


class CatalogUnavailable(ValueError):
    """Safe discovery failure without provider response details."""


async def discover_models(repo: ProviderRepository, body: dict) -> dict:
    kind, base, token = validate_connection(body)
    keyless = body.get("keyless", False)
    if type(keyless) is not bool or (
        keyless and (token or kind not in {"openai", "openai-responses"})
    ):
        raise ValueError("Invalid keyless model discovery")
    channel = body.get("existing_channel")
    if token is None and channel is not None and not keyless:
        saved = repo.profile(channel)
        if saved.get("oauth_id"):
            raise ValueError("Account discovery requires account authorization")
        token = saved.get("api_key")
        if token and (saved["provider"], saved["base_url"]) != (kind, base):
            raise ValueError(
                "Enter an API key to discover models at a changed endpoint"
            )
    try:
        return await _fetch_models(
            kind,
            base,
            token or "",
            allow_private_http=body.get("allow_private_http", False),
        )
    except httpx.HTTPError, TimeoutError, json.JSONDecodeError, UnicodeError:
        raise CatalogUnavailable(
            "Could not retrieve model list. Check the endpoint and API key, or enter models manually."
        ) from None


REFRESH_INTERVAL = 600


async def prepare_auto_save(
    repo: ProviderRepository, value: dict, previous: dict
) -> dict | None:
    """Probe before opening the write transaction; caller must check snapshot."""
    if previous.get("oauth_id"):
        raise ValueError("Account discovery requires account authorization")
    same_endpoint = (previous.get("provider"), previous.get("base_url")) == (
        value["provider"],
        value["base_url"],
    )
    supplied = value.get("api_key")
    if value.get("keyless"):
        token = ""
    elif supplied is not None:
        token = supplied
    elif previous.get("api_key_ciphertext"):
        if not same_endpoint:
            raise ValueError(
                "Enter an API key to use automatic discovery at a changed endpoint"
            )
        token = repo._secret(previous)
    else:
        token = ""
    try:
        return await _fetch_models(
            value["provider"],
            value["base_url"],
            token,
            allow_empty=previous.get("auto_discover") is True and same_endpoint,
            allow_private_http=value.get("allow_private_http", False),
        )
    except httpx.HTTPError, TimeoutError, ValueError, UnicodeError:
        if same_endpoint and (
            previous.get("models") or previous.get("auto_discover") is True
        ):
            try:
                if token == repo._secret(previous):
                    return None
            except ValueError:
                pass
        raise CatalogUnavailable(
            "Unable to discover models. Check the connection or turn off automatic discovery."
        ) from None


async def save_discovered(repo: ProviderRepository, body: dict) -> dict:
    """Probe outside the write transaction, then commit the exact snapshot."""
    value = _validate(body)
    if not value["auto_discover"]:
        return repo._save_validated(body, value)
    old_name, key, previous_key = repo._save_keys(body, value)
    with repo.database.read_snapshot() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE owner=? AND key=?",
            (_OWNER, previous_key),
        ).fetchone()
    raw = row[0] if row is not None else None
    previous = json.loads(raw) if raw is not None else {}
    if old_name is not None and key != previous_key and raw is None:
        raise KeyError("Original channel no longer exists")
    if previous and previous["provider"] != value["provider"]:
        raise ChannelConflict(
            "Existing channel provider type cannot be changed"
        )
    catalog = await prepare_auto_save(repo, value, previous)
    return repo._save_validated(body, value, prepared=(raw, catalog))


async def _refresh_snapshot(
    repo: ProviderRepository, key: str, raw: str
) -> bool:
    """No network under write lock; compare exact prior row before updating."""
    value = json.loads(raw)
    if value.get("auto_discover") is not True:
        return False
    try:
        if value.get("oauth_id"):
            raise ValueError("Account discovery requires account authorization")
        kind, base, _ = validate_connection(value)
        catalog = await _fetch_models(
            kind,
            base,
            repo._secret(value),
            allow_empty=True,
            allow_private_http=value.get("allow_private_http", False),
        )
    except httpx.HTTPError, TimeoutError, ValueError, UnicodeError:
        value["model_sync_error"] = _SYNC_ERROR
    else:
        _apply_catalog(
            value, catalog, value.get("model_context_overrides") or {}
        )
    with repo.database.connect() as db:
        cursor = db.execute(
            "UPDATE settings SET value=? WHERE owner=? AND key=? AND value=?",
            (json.dumps(value), _OWNER, key, raw),
        )
        return cursor.rowcount == 1


AccountRefresh = Callable[[ProviderRepository, str, str], Awaitable[bool]]


async def _refresh_channel_snapshot(
    repo: ProviderRepository,
    key: str,
    raw: str,
    account_refresh: AccountRefresh | None,
) -> bool:
    if json.loads(raw).get("oauth_id"):
        if account_refresh is None:
            return False
        return await account_refresh(repo, key, raw)
    return await _refresh_snapshot(repo, key, raw)


async def refresh_model_channels(
    repo: ProviderRepository, *, account_refresh: AccountRefresh | None = None
) -> None:
    with repo.database.read_snapshot() as db:
        rows = db.execute(
            "SELECT key,value FROM settings WHERE owner=? AND (key LIKE ? OR key LIKE ?) AND json_extract(value,'$.auto_discover')=1",
            (_OWNER, _PREFIX + "%", _CHANNEL_PREFIX + "%"),
        ).fetchall()
    semaphore = asyncio.Semaphore(4)

    async def refresh(row):
        async with semaphore:
            await _refresh_channel_snapshot(
                repo, row["key"], row["value"], account_refresh
            )

    results = await asyncio.gather(
        *(refresh(row) for row in rows), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception):
            logger.warning(
                "Model catalog refresh failed: %s", type(result).__name__
            )


async def serve_model_refresh(
    repo: ProviderRepository, *, account_refresh: AccountRefresh | None = None
) -> None:
    while True:
        try:
            if account_refresh is None:
                await refresh_model_channels(repo)
            else:
                await refresh_model_channels(
                    repo, account_refresh=account_refresh
                )
        except Exception as exc:
            logger.warning(
                "Model catalog refresh cycle failed: %s", type(exc).__name__
            )
        await asyncio.sleep(REFRESH_INTERVAL)


async def refresh_model_channel(
    repo: ProviderRepository,
    channel: str,
    *,
    account_refresh: AccountRefresh | None = None,
) -> dict:
    if not isinstance(channel, str):
        raise ValueError("Specify a saved channel")
    key = (
        _PREFIX + channel if channel in _DEFAULTS else _CHANNEL_PREFIX + channel
    )
    with repo.database.read_snapshot() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE owner=? AND key=?", (_OWNER, key)
        ).fetchone()
    if row is None:
        raise KeyError("Channel not found")
    value = json.loads(row[0])
    if value.get("auto_discover") is not True:
        raise ValueError("Automatic model discovery is disabled")
    if value.get("oauth_id") and account_refresh is None:
        raise ValueError("Account discovery requires account authorization")
    if not await _refresh_channel_snapshot(repo, key, row[0], account_refresh):
        raise ValueError("Channel changed during refresh; retry")
    return repo.public_profiles()
