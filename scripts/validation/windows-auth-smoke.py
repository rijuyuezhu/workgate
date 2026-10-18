"""Native Windows Agent Bridge credentials and MCP manifest security smoke."""

import json
import os
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("This smoke requires native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.agent_bridge.auth_store import (  # noqa: E402
    AgentAuthStore,
    AgentAuthStoreCorruptError,
)
from workgate.agent_bridge.management import (  # noqa: E402
    ManagedMcpConfig,
    manage_mcp_manifest,
)
from workgate.platform.windows.security import open_private_file  # noqa: E402
from workgate.utils.private_files import read_private_bytes  # noqa: E402


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-native-auth-") as tmp:
        root = Path(tmp) / "auth"
        store = AgentAuthStore(root)
        assert store.list_secrets() == {}
        store.set_secret("docs", "api_token", "sensitive-value")
        assert store.get_secret("docs", "api_token") == "sensitive-value"
        assert store.list_secrets("docs") == {"docs": ["api_token"]}
        with os.fdopen(
            open_private_file(root / "credentials.json", create=False), "rb"
        ) as reader:
            assert json.load(reader)["version"] == 1
        assert store.delete_secret("docs", "api_token")
        assert not store.delete_secret("docs", "api_token")

        # A permissively inherited file must not be read as a private store.
        insecure = root / "unprotected.json"
        insecure.write_bytes(b'{"version": 1}')
        try:
            read_private_bytes(insecure)
        except PermissionError:
            pass
        else:
            raise AssertionError(
                "Insecure Windows private file DACL was accepted"
            )
        link = root / "credentials-link.json"
        try:
            link.symlink_to(root / "credentials.json")
        except OSError:
            pass
        else:
            try:
                read_private_bytes(link)
            except PermissionError:
                pass
            else:
                raise AssertionError("Symlink to credentials was followed")
            link.unlink()

        store.path.write_text("{broken", encoding="utf-8")
        try:
            store.list_secrets()
        except AgentAuthStoreCorruptError:
            assert store.path.read_bytes() == b"{broken"
        else:
            raise AssertionError("Corrupted credentials were silently reset")

        config = Path(tmp) / "mcp"
        first = ManagedMcpConfig(type="http", url="https://example.org/mcp")
        assert manage_mcp_manifest(
            config, "register", name="docs", config=first, owner_type="network"
        )["updated"]
        assert (
            manage_mcp_manifest(config, "list", owner_type="network")[
                "servers"
            ][0]["name"]
            == "docs"
        )
        current = manage_mcp_manifest(
            config, "get", name="docs", owner_type="network"
        )["server"]
        assert manage_mcp_manifest(
            config,
            "disable",
            name="docs",
            owner_type="network",
            expected_revision=current["revision"],
        )["updated"]
        disabled = manage_mcp_manifest(
            config, "get", name="docs", owner_type="network"
        )["server"]
        assert not disabled["enabled"]
        assert manage_mcp_manifest(
            config,
            "remove",
            name="docs",
            owner_type="network",
            expected_revision=disabled["revision"],
        )["updated"]
        with os.fdopen(
            open_private_file(config / "config.json", create=False), "rb"
        ) as reader:
            assert not json.load(reader).get("mcpServers")
    print("Windows native Agent secrets and MCP management: PASS")


if __name__ == "__main__":
    run()
