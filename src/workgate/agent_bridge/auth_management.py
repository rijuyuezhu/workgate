"""Owner-side Agent Bridge MCP authentication metadata and secret mutation.

Never return secret values, OAuth tokens, callbacks, or unvalidated server URLs.
Interactive OAuth authorization remains the existing local CLI until a secure
browser callback lifecycle is explicitly implemented.
"""

from pathlib import Path
from typing import Any, Literal

from .auth import credential_store_key, oauth_status
from .auth_store import AgentAuthStore
from .state import load_agent_manifest


def manage_mcp_auth(
    config_dir: Path,
    auth_dir: Path,
    action: str,
    *,
    name: str,
    owner_type: Literal["network", "stdio"],
    secret_name: str | None = None,
    secret_value: str | None = None,
) -> dict[str, Any]:
    """Authorize one configured server on its owning host; metadata only."""
    if action not in {"status", "set_secret", "delete_secret", "clear_oauth"}:
        raise ValueError("Unsupported MCP auth action")
    if not name or len(name) > 128:
        raise ValueError("A configured MCP server name is required")
    loaded = load_agent_manifest(config_dir, read_only=True)
    if loaded.status not in {"loaded", "missing_config"}:
        raise ValueError("Invalid MCP manifest")
    server = loaded.data.mcp_servers.get(name)
    if server is None or (server.type == "stdio") != (owner_type == "stdio"):
        raise ValueError("MCP server is not configured on this owner")
    if action in {"set_secret", "delete_secret"}:
        if not secret_name or len(secret_name) > 128:
            raise ValueError("Secret reference name is required")
        if action == "set_secret" and (
            not isinstance(secret_value, str)
            or not secret_value
            or len(secret_value.encode("utf-8")) > 65_536
        ):
            raise ValueError("Invalid or oversized secret value")
    elif secret_name is not None or secret_value is not None:
        raise ValueError("Secret parameters do not belong to this auth action")
    if action == "clear_oauth" and server.auth.mode != "oauth":
        raise ValueError("OAuth credentials are not configured for this MCP")
    store = AgentAuthStore(auth_dir)
    identity = credential_store_key(name, server)
    changed = None
    if action == "set_secret":
        assert secret_name is not None and secret_value is not None
        store.set_secret(identity, secret_name, secret_value)
        changed = True
    elif action == "delete_secret":
        assert secret_name is not None
        changed = store.delete_secret(identity, secret_name)
    elif action == "clear_oauth":
        # Do not claim remote token revocation: this only drops local OAuth state.
        changed = store.clear_oauth(identity)
    result: dict[str, Any] = {
        "name": name,
        "auth": oauth_status(store, name, server),
        "secret_names": store.list_secrets(identity).get(identity, []),
    }
    if changed is not None:
        result["changed"] = changed
    return result
