"""MCP private authentication management reuses the owner-specific store."""

import json

import pytest

from workgate.agent_bridge.auth_management import manage_mcp_auth


def config(tmp_path, *, mode="none"):
    root = tmp_path / "agent"
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.org/mcp",
                        "auth": {"mode": mode},
                        "integrationId": "remote-id",
                    },
                    "local": {"type": "stdio", "command": "echo"},
                },
            }
        )
    )
    return root, tmp_path / "secrets"


def test_private_secret_updates_and_local_oauth_clear(tmp_path):
    root, store = config(tmp_path, mode="oauth")
    result = manage_mcp_auth(
        root, store, "status", name="remote", owner_type="network"
    )
    assert result["auth"]["status"] == "unauthorized"
    assert result["secret_names"] == []
    stored = manage_mcp_auth(
        root,
        store,
        "set_secret",
        name="remote",
        owner_type="network",
        secret_name="key",
        secret_value="NEVER_SHOW_PRIVATE",
    )
    assert stored["changed"] is True
    assert stored["secret_names"] == ["key"]
    assert "NEVER_SHOW_PRIVATE" not in json.dumps(stored)
    cleared = manage_mcp_auth(
        root, store, "clear_oauth", name="remote", owner_type="network"
    )
    assert cleared["changed"] is False
    assert cleared["auth"]["status"] == "unauthorized"
    deleted = manage_mcp_auth(
        root,
        store,
        "delete_secret",
        name="remote",
        owner_type="network",
        secret_name="key",
    )
    assert deleted["changed"] is True
    assert deleted["secret_names"] == []
    assert (
        manage_mcp_auth(
            root,
            store,
            "delete_secret",
            name="remote",
            owner_type="network",
            secret_name="key",
        )["changed"]
        is False
    )


def test_untrusted_invalid_actions_names_content_owner_and_manifest(tmp_path):
    root, store = config(tmp_path)
    invalid = [
        ("unexpected", {"name": "remote", "owner_type": "network"}),
        ("status", {"name": "x" * 129, "owner_type": "network"}),
        ("status", {"name": "missing", "owner_type": "network"}),
        ("status", {"name": "remote", "owner_type": "stdio"}),
        (
            "status",
            {"name": "remote", "owner_type": "network", "secret_name": "x"},
        ),
        (
            "set_secret",
            {"name": "remote", "owner_type": "network", "secret_name": "key"},
        ),
        (
            "set_secret",
            {
                "name": "remote",
                "owner_type": "network",
                "secret_name": "key",
                "secret_value": "",
            },
        ),
        (
            "set_secret",
            {
                "name": "remote",
                "owner_type": "network",
                "secret_name": "key",
                "secret_value": "x" * (65_536 + 1),
            },
        ),
        ("delete_secret", {"name": "remote", "owner_type": "network"}),
        ("clear_oauth", {"name": "remote", "owner_type": "network"}),
    ]
    for action, fields in invalid:
        with pytest.raises(ValueError):
            manage_mcp_auth(
                root,
                store,
                action,
                name=fields.get("name", ""),
                owner_type="stdio"
                if fields.get("owner_type") == "stdio"
                else "network",
                secret_name=fields.get("secret_name"),
                secret_value=fields.get("secret_value"),
            )
    (root / "config.json").write_text('{"version": 999}')
    with pytest.raises(ValueError, match="Invalid MCP manifest"):
        manage_mcp_auth(
            root, store, "status", name="remote", owner_type="network"
        )
    (root / "config.json").unlink()
    with pytest.raises(ValueError, match="not configured"):
        manage_mcp_auth(
            root, store, "status", name="remote", owner_type="network"
        )
