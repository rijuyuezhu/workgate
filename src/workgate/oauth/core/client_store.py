"""Persistent storage for locally approved OAuth clients."""

import json
from collections.abc import Mapping, MutableMapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...config.settings import get_settings
from ...persistence import FileStateStore, StateLayout, StateStore
from .models import OAuthClient

CLIENT_STORE_FILENAME = "oauth-clients.json"
CLIENT_STORE_VERSION = 1


def client_store_path(*, state_dir: Path | None = None) -> Path:
    """Return the configured persistent OAuth client registry path."""
    root = get_settings().state_dir if state_dir is None else state_dir
    return StateLayout(root).oauth_clients_path


def _state_store(
    *, state_store: StateStore | None, state_dir: Path | None
) -> StateStore:
    if state_store is not None:
        return state_store
    root = get_settings().state_dir if state_dir is None else state_dir
    return FileStateStore(lambda: root)


def _decode_client(raw: object) -> OAuthClient:
    """Validate and decode one client registry record."""
    if not isinstance(raw, dict):
        raise ValueError("OAuth client record must be an object")
    data: dict[str, Any] = raw
    client_id = data.get("client_id")
    redirect_uris = data.get("redirect_uris")
    client_name = data.get("client_name")
    created_at = data.get("created_at")
    approved_at = data.get("approved_at")
    if not isinstance(client_id, str) or not client_id:
        raise ValueError("OAuth client record has an invalid client_id")
    if not isinstance(redirect_uris, list) or not all(
        isinstance(uri, str) and uri for uri in redirect_uris
    ):
        raise ValueError("OAuth client record has invalid redirect_uris")
    if client_name is not None and not isinstance(client_name, str):
        raise ValueError("OAuth client record has an invalid client_name")
    if not isinstance(created_at, int):
        raise ValueError("OAuth client record has an invalid created_at")
    if not isinstance(approved_at, int) or approved_at < created_at:
        raise ValueError("OAuth client record has an invalid approved_at")
    return OAuthClient(
        client_id=client_id,
        redirect_uris=redirect_uris,
        client_name=client_name,
        created_at=created_at,
        approved_at=approved_at,
    )


def load_persisted_clients(
    clients: MutableMapping[str, OAuthClient],
    *,
    state_store: StateStore | None = None,
    state_dir: Path | None = None,
) -> int:
    """Merge persisted approved clients into one explicit in-memory registry."""
    store = _state_store(state_store=state_store, state_dir=state_dir)
    path = store.layout.oauth_clients_path
    try:
        with store.transaction(path):
            payload = store.read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Unable to read OAuth client registry: {path}"
        ) from exc
    if payload is None:
        return 0
    if (
        not isinstance(payload, dict)
        or payload.get("version") != CLIENT_STORE_VERSION
    ):
        raise RuntimeError(f"Unsupported OAuth client registry format: {path}")
    records = payload.get("clients")
    if not isinstance(records, list):
        raise RuntimeError(f"Invalid OAuth client registry contents: {path}")

    loaded = 0
    for raw in records:
        try:
            client = _decode_client(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid OAuth client registry contents: {path}"
            ) from exc
        current = clients.get(client.client_id)
        if current is None or current.created_at <= client.created_at:
            clients[client.client_id] = client
            loaded += 1
    return loaded


def persist_approved_clients(
    clients: Mapping[str, OAuthClient],
    *,
    state_store: StateStore | None = None,
    state_dir: Path | None = None,
) -> None:
    """Atomically write locally approved clients to disk."""
    store = _state_store(state_store=state_store, state_dir=state_dir)
    path = store.layout.oauth_clients_path
    approved_clients = (
        clients[key]
        for key in sorted(clients)
        if clients[key].approved_at is not None
    )
    payload = {
        "version": CLIENT_STORE_VERSION,
        "clients": [asdict(client) for client in approved_clients],
    }
    with store.transaction(path):
        store.write_json(path, payload)
