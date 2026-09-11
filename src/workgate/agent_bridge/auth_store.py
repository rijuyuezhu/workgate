"""Private credential persistence for Agent Bridge secrets and OAuth clients."""

import contextlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from mcp.client.auth.oauth2 import TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
)

from ..utils.private_files import atomic_write_private_text, private_file_lock

_STORE_VERSION = 1
_MAX_STORE_BYTES = 1_048_576
_MAX_SECRET_BYTES = 65_536
_MAX_REDACTION_JOURNAL_ENTRIES = 256
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class AgentAuthStoreError(RuntimeError):
    """Base error for private Agent Bridge credential persistence."""


class AgentAuthStoreCorruptError(AgentAuthStoreError):
    """The private credential file is unreadable or has an invalid schema."""


class AgentSecretNotFoundError(AgentAuthStoreError):
    """A configured secret reference is missing from the private store."""


class AgentAuthRedactionHistoryLostError(AgentAuthStoreError):
    """Credential mutations cannot be reconstructed safely for redaction."""


def _validate_name(value: str, label: str) -> str:
    value = str(value)
    if not _NAME_RE.fullmatch(value):
        raise ValueError(
            f"{label} must be 1-128 characters using letters, digits, '.', '_', or '-'"
        )
    return value


def _empty_store() -> dict[str, Any]:
    return {
        "version": _STORE_VERSION,
        "servers": {},
        "credential_revisions": {},
    }


class AgentAuthStore:
    """Versioned, bounded, owner-only credential store with atomic mutation."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / "credentials.json"
        self.lock_path = self.root / "credentials.lock"
        self._thread_lock = threading.RLock()
        self._redaction_journal: dict[
            str, list[tuple[int, tuple[str, ...]]]
        ] = {}
        self._ensure_private_dir()

    def _record_redaction_values_unlocked(
        self, server: str, revision: int, values: tuple[str, ...]
    ) -> None:
        values = tuple(dict.fromkeys(value for value in values if value))
        journal = self._redaction_journal.setdefault(server, [])
        journal.append((revision, values))
        if len(journal) > _MAX_REDACTION_JOURNAL_ENTRIES:
            del journal[: len(journal) - _MAX_REDACTION_JOURNAL_ENTRIES]

    @staticmethod
    def _credential_revision_unlocked(data: dict[str, Any], server: str) -> int:
        revisions = data.get("credential_revisions") or {}
        return int(revisions.get(server, 0))

    @classmethod
    def _bump_credential_revision_unlocked(
        cls, data: dict[str, Any], server: str
    ) -> int:
        revisions = data.setdefault("credential_revisions", {})
        revision = cls._credential_revision_unlocked(data, server) + 1
        revisions[server] = revision
        return revision

    def _mutate_with_redaction(self, server: str, mutate) -> bool:
        with self._thread_lock, private_file_lock(self.lock_path):
            data = self._read_unlocked()
            changed, values = mutate(data)
            revision = (
                self._bump_credential_revision_unlocked(data, server)
                if changed
                else None
            )
            self._prune(data)
            self._write_unlocked(data)
            if revision is not None:
                self._record_redaction_values_unlocked(
                    server, revision, tuple(values)
                )
            return changed

    def _ensure_private_dir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            self.root.chmod(0o700)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_store()
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise AgentAuthStoreError(
                f"unable to inspect Agent Bridge credential store: {exc}"
            ) from exc
        if size > _MAX_STORE_BYTES:
            raise AgentAuthStoreCorruptError(
                f"Agent Bridge credential store exceeds {_MAX_STORE_BYTES} bytes"
            )
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentAuthStoreCorruptError(
                f"Agent Bridge credential store is invalid: {exc}"
            ) from exc
        self._validate_store(data)
        return data

    @staticmethod
    def _validate_store(data: Any) -> None:
        if not isinstance(data, dict) or data.get("version") != _STORE_VERSION:
            raise AgentAuthStoreCorruptError(
                "Agent Bridge credential store has an unsupported schema version"
            )
        servers = data.get("servers")
        if not isinstance(servers, dict):
            raise AgentAuthStoreCorruptError(
                "Agent Bridge credential store servers must be an object"
            )
        revisions = data.get("credential_revisions", {})
        if not isinstance(revisions, dict):
            raise AgentAuthStoreCorruptError(
                "Agent Bridge credential revisions must be an object"
            )
        for server_name, revision in revisions.items():
            try:
                _validate_name(server_name, "server name")
            except ValueError as exc:
                raise AgentAuthStoreCorruptError(str(exc)) from exc
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise AgentAuthStoreCorruptError(
                    f"Agent Bridge credential revision for {server_name} must be a non-negative integer"
                )
        for server_name, entry in servers.items():
            try:
                _validate_name(server_name, "server name")
            except ValueError as exc:
                raise AgentAuthStoreCorruptError(str(exc)) from exc
            if not isinstance(entry, dict):
                raise AgentAuthStoreCorruptError(
                    f"Agent Bridge credential entry for {server_name} must be an object"
                )
            secrets = entry.get("secrets", {})
            if not isinstance(secrets, dict):
                raise AgentAuthStoreCorruptError(
                    f"Agent Bridge secrets for {server_name} must be an object"
                )
            for secret_name, secret_value in secrets.items():
                try:
                    _validate_name(secret_name, "secret name")
                except ValueError as exc:
                    raise AgentAuthStoreCorruptError(str(exc)) from exc
                if not isinstance(secret_value, str):
                    raise AgentAuthStoreCorruptError(
                        f"Agent Bridge secret {server_name}/{secret_name} must be a string"
                    )
                if len(secret_value.encode("utf-8")) > _MAX_SECRET_BYTES:
                    raise AgentAuthStoreCorruptError(
                        f"Agent Bridge secret {server_name}/{secret_name} is too large"
                    )
            oauth = entry.get("oauth")
            if oauth is not None:
                if not isinstance(oauth, dict):
                    raise AgentAuthStoreCorruptError(
                        f"Agent Bridge OAuth state for {server_name} must be an object"
                    )
                for key in ("tokens", "client_info", "authorization_metadata"):
                    value = oauth.get(key)
                    if value is not None and not isinstance(value, dict):
                        raise AgentAuthStoreCorruptError(
                            f"Agent Bridge OAuth {key} for {server_name} must be an object"
                        )
                for key in ("stored_at", "expires_at"):
                    value = oauth.get(key)
                    if value is not None and not isinstance(
                        value, (int, float)
                    ):
                        raise AgentAuthStoreCorruptError(
                            f"Agent Bridge OAuth {key} for {server_name} must be numeric"
                        )

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self._validate_store(data)
        encoded = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > _MAX_STORE_BYTES:
            raise AgentAuthStoreError(
                f"Agent Bridge credential store would exceed {_MAX_STORE_BYTES} bytes"
            )
        atomic_write_private_text(self.path, encoded + "\n")

    def _read(self) -> dict[str, Any]:
        with self._thread_lock, private_file_lock(self.lock_path):
            return self._read_unlocked()

    def _mutate(self, mutate) -> Any:
        with self._thread_lock, private_file_lock(self.lock_path):
            data = self._read_unlocked()
            result = mutate(data)
            self._prune(data)
            self._write_unlocked(data)
            return result

    @staticmethod
    def _prune(data: dict[str, Any]) -> None:
        servers = data["servers"]
        for name in list(servers):
            entry = servers[name]
            if not entry.get("secrets"):
                entry.pop("secrets", None)
            oauth = entry.get("oauth")
            if isinstance(oauth, dict) and not any(
                oauth.get(key) is not None
                for key in ("tokens", "client_info", "authorization_metadata")
            ):
                entry.pop("oauth", None)
            if not entry:
                del servers[name]

    @staticmethod
    def _server_entry(data: dict[str, Any], server: str) -> dict[str, Any]:
        return data["servers"].setdefault(server, {})

    def set_secret(self, server: str, name: str, value: str) -> None:
        """Store one bounded private secret for a configured server."""
        server = _validate_name(server, "server name")
        name = _validate_name(name, "secret name")
        if not isinstance(value, str):
            raise TypeError("secret value must be text")
        if not value:
            raise ValueError("secret value must not be empty")
        if len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
            raise ValueError(f"secret value exceeds {_MAX_SECRET_BYTES} bytes")

        def mutate(data: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
            entry = self._server_entry(data, server)
            entry.setdefault("secrets", {})[name] = value
            return True, (value,)

        self._mutate_with_redaction(server, mutate)

    def get_secret(self, server: str, name: str) -> str:
        """Return one private secret or raise a non-disclosing missing error."""
        server = _validate_name(server, "server name")
        name = _validate_name(name, "secret name")
        data = self._read()
        try:
            return data["servers"][server]["secrets"][name]
        except KeyError as exc:
            raise AgentSecretNotFoundError(
                f"missing Agent Bridge secret for server {server}"
            ) from exc

    def has_secret(self, server: str, name: str) -> bool:
        """Return whether one private secret exists."""
        try:
            self.get_secret(server, name)
        except AgentSecretNotFoundError:
            return False
        return True

    def list_secrets(self, server: str | None = None) -> dict[str, list[str]]:
        """List secret names without loading or exposing their values."""
        data = self._read()
        if server is not None:
            server = _validate_name(server, "server name")
            entries = {server: data["servers"].get(server, {})}
        else:
            entries = data["servers"]
        return {
            name: sorted(entry.get("secrets", {}))
            for name, entry in sorted(entries.items())
            if entry.get("secrets")
        }

    def delete_secret(self, server: str, name: str) -> bool:
        """Delete one private secret and report whether it existed."""
        server = _validate_name(server, "server name")
        name = _validate_name(name, "secret name")

        def mutate(data: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
            secrets = data["servers"].get(server, {}).get("secrets", {})
            removed = secrets.pop(name, None)
            return removed is not None, (
                (removed,) if removed is not None else ()
            )

        return self._mutate_with_redaction(server, mutate)

    def get_tokens(self, server: str) -> OAuthToken | None:
        """Load persisted OAuth tokens for one server."""
        server = _validate_name(server, "server name")
        oauth = self._read()["servers"].get(server, {}).get("oauth", {})
        payload = oauth.get("tokens")
        if payload is None:
            return None
        try:
            return OAuthToken.model_validate(payload)
        except Exception as exc:
            raise AgentAuthStoreCorruptError(
                f"Agent Bridge OAuth tokens for {server} are invalid"
            ) from exc

    def oauth_redaction_values(self, server: str) -> dict[str, str]:
        """Return private OAuth credential values owned by this store for redaction only."""
        values: dict[str, str] = {}
        tokens = self.get_tokens(server)
        if tokens is not None:
            for field in ("access_token", "refresh_token"):
                value = getattr(tokens, field, None)
                if value:
                    values[f"oauth_{field}"] = str(value)
        client_info = self.get_client_info(server)
        if client_info is not None:
            client_secret = getattr(client_info, "client_secret", None)
            if client_secret:
                values["oauth_client_secret"] = str(client_secret)
        return values

    def redaction_cursor(self, server: str) -> int:
        """Return the durable credential revision at operation start."""
        server = _validate_name(server, "server name")
        with self._thread_lock, private_file_lock(self.lock_path):
            data = self._read_unlocked()
            return self._credential_revision_unlocked(data, server)

    def credential_redaction_values_since(
        self, server: str, cursor: int
    ) -> dict[str, str]:
        """Return locally observed credential mutations, failing closed on gaps."""
        server = _validate_name(server, "server name")
        if not isinstance(cursor, int) or cursor < 0:
            raise ValueError("redaction cursor must be a non-negative integer")
        with self._thread_lock, private_file_lock(self.lock_path):
            data = self._read_unlocked()
            revision = self._credential_revision_unlocked(data, server)
            if cursor > revision:
                raise ValueError(
                    "redaction cursor is newer than credential history"
                )
            journal = self._redaction_journal.get(server, [])
            observed = {
                entry_revision: entry_values
                for entry_revision, entry_values in journal
                if cursor < entry_revision <= revision
            }
            observed_revisions = sorted(observed)
            if len(observed_revisions) != revision - cursor or any(
                entry_revision != cursor + offset
                for offset, entry_revision in enumerate(
                    observed_revisions, start=1
                )
            ):
                raise AgentAuthRedactionHistoryLostError(
                    f"credential redaction history unavailable for server {server}"
                )
            values = tuple(
                dict.fromkeys(
                    value
                    for entry_revision in observed_revisions
                    for value in observed[entry_revision]
                )
            )
        return {
            f"credential_observed_{index}": value
            for index, value in enumerate(values)
        }

    def set_tokens(self, server: str, tokens: OAuthToken) -> None:
        """Persist OAuth tokens and their absolute expiry timestamp."""
        server = _validate_name(server, "server name")
        now = time.time()

        def mutate(data: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
            entry = self._server_entry(data, server)
            oauth = entry.setdefault("oauth", {})
            previous = oauth.get("tokens") or {}
            payload = tokens.model_dump(mode="json", exclude_none=True)
            if "refresh_token" not in payload and previous.get("refresh_token"):
                payload["refresh_token"] = previous["refresh_token"]
            oauth["tokens"] = payload
            oauth["stored_at"] = now
            oauth["expires_at"] = (
                now + tokens.expires_in
                if tokens.expires_in is not None
                else None
            )
            return True, tuple(
                str(payload[field])
                for field in ("access_token", "refresh_token")
                if payload.get(field)
            )

        self._mutate_with_redaction(server, mutate)

    def get_client_info(self, server: str) -> OAuthClientInformationFull | None:
        """Load persisted OAuth dynamic client information."""
        server = _validate_name(server, "server name")
        oauth = self._read()["servers"].get(server, {}).get("oauth", {})
        payload = oauth.get("client_info")
        if payload is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(payload)
        except Exception as exc:
            raise AgentAuthStoreCorruptError(
                f"Agent Bridge OAuth client information for {server} is invalid"
            ) from exc

    def set_client_info(
        self, server: str, client_info: OAuthClientInformationFull
    ) -> None:
        """Persist OAuth dynamic client information."""
        server = _validate_name(server, "server name")

        def mutate(data: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
            entry = self._server_entry(data, server)
            oauth = entry.setdefault("oauth", {})
            oauth["client_info"] = client_info.model_dump(
                mode="json", exclude_none=True
            )
            client_secret = getattr(client_info, "client_secret", None)
            return True, ((str(client_secret),) if client_secret else ())

        self._mutate_with_redaction(server, mutate)

    def get_authorization_metadata(self, server: str) -> OAuthMetadata | None:
        """Load discovered public OAuth authorization-server metadata."""
        server = _validate_name(server, "server name")
        oauth = self._read()["servers"].get(server, {}).get("oauth", {})
        payload = oauth.get("authorization_metadata")
        if payload is None:
            return None
        try:
            return OAuthMetadata.model_validate(payload)
        except Exception as exc:
            raise AgentAuthStoreCorruptError(
                f"Agent Bridge OAuth authorization metadata for {server} is invalid"
            ) from exc

    def set_authorization_metadata(
        self, server: str, metadata: OAuthMetadata
    ) -> None:
        """Persist discovered public OAuth authorization-server metadata."""
        server = _validate_name(server, "server name")

        def mutate(data: dict[str, Any]) -> None:
            entry = self._server_entry(data, server)
            oauth = entry.setdefault("oauth", {})
            oauth["authorization_metadata"] = metadata.model_dump(
                mode="json", exclude_none=True
            )

        self._mutate(mutate)

    def oauth_metadata(self, server: str) -> dict[str, Any]:
        """Return non-sensitive OAuth status metadata for one server."""
        server = _validate_name(server, "server name")
        oauth = self._read()["servers"].get(server, {}).get("oauth", {})
        tokens = oauth.get("tokens") or {}
        return {
            "has_access_token": bool(tokens.get("access_token")),
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "client_registered": bool(oauth.get("client_info")),
            "stored_at": oauth.get("stored_at"),
            "expires_at": oauth.get("expires_at"),
        }

    def oauth_expiry(self, server: str) -> float | None:
        """Return the persisted absolute access-token expiry timestamp."""
        return self.oauth_metadata(server).get("expires_at")

    def clear_tokens(self, server: str) -> bool:
        """Remove stale tokens while retaining client and discovery metadata."""
        server = _validate_name(server, "server name")

        def mutate(data: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
            oauth = data["servers"].get(server, {}).get("oauth", {})
            tokens = oauth.get("tokens") or {}
            values = tuple(
                str(tokens[field])
                for field in ("access_token", "refresh_token")
                if tokens.get(field)
            )
            removed = oauth.pop("tokens", None) is not None
            oauth.pop("stored_at", None)
            oauth.pop("expires_at", None)
            return removed, values

        return self._mutate_with_redaction(server, mutate)

    def clear_oauth(self, server: str) -> bool:
        """Remove all local OAuth state for one server."""
        server = _validate_name(server, "server name")

        def mutate(data: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
            entry = data["servers"].get(server, {})
            oauth = entry.get("oauth") or {}
            tokens = oauth.get("tokens") or {}
            client_info = oauth.get("client_info") or {}
            values = tuple(
                str(value)
                for value in (
                    tokens.get("access_token"),
                    tokens.get("refresh_token"),
                    client_info.get("client_secret"),
                )
                if value
            )
            return entry.pop("oauth", None) is not None, values

        return self._mutate_with_redaction(server, mutate)

    def fingerprint_paths(self) -> tuple[Path, ...]:
        """Return stable paths whose metadata/content changes trigger bridge reload."""
        return (self.path,)


class AgentOAuthTokenStorage(TokenStorage):
    """MCP SDK TokenStorage adapter scoped to one Agent Bridge server."""

    def __init__(self, store: AgentAuthStore, server: str) -> None:
        self.store = store
        self.server = _validate_name(server, "server name")

    async def get_tokens(self) -> OAuthToken | None:
        """Load tokens through the MCP SDK storage protocol."""
        return self.store.get_tokens(self.server)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist tokens through the MCP SDK storage protocol."""
        self.store.set_tokens(self.server, tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Load client information through the MCP SDK storage protocol."""
        return self.store.get_client_info(self.server)

    async def set_client_info(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        """Persist client information through the MCP SDK storage protocol."""
        self.store.set_client_info(self.server, client_info)

    def expiry(self) -> float | None:
        """Return the persisted absolute token expiry for provider restore."""
        return self.store.oauth_expiry(self.server)

    def authorization_metadata(self) -> OAuthMetadata | None:
        """Return persisted authorization-server metadata for provider restore."""
        return self.store.get_authorization_metadata(self.server)

    def set_authorization_metadata(self, metadata: OAuthMetadata) -> None:
        """Persist authorization-server metadata discovered by the provider."""
        self.store.set_authorization_metadata(self.server, metadata)
