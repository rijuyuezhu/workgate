import json
import os

import pytest

from workgate.agent_bridge.models import (
    AgentBridgeManifest,
    AgentDynamicToolsConfig,
    AgentMcpServerConfig,
    AgentSecretReference,
)
from workgate.agent_bridge.redaction import _redact_text, redact_mapping
from workgate.agent_bridge.state import load_agent_manifest
from workgate.app_paths import app_paths
from workgate.config.settings import (
    AGENT_AUTH_STATE_DIR_NAME,
    AUDIT_LOG_STATE_DIR_NAME,
    clear_settings_cache,
    get_settings,
    load_settings,
)


def test_workspace_root_does_not_rewrite_default_state_paths(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("WORKGATE_STATE_DIR", raising=False)
    clear_settings_cache()

    settings = load_settings()
    default_state_dir = app_paths().state_dir

    assert settings.workspace_root == tmp_path.resolve()
    assert settings.state_dir == default_state_dir.resolve()
    assert (
        settings.audit_log_path
        == (
            default_state_dir / AUDIT_LOG_STATE_DIR_NAME / "audit.jsonl"
        ).resolve()
    )
    assert settings.agent_config_dir == app_paths().agent_config_dir.resolve()
    assert (
        settings.agent_auth_dir
        == (default_state_dir / AGENT_AUTH_STATE_DIR_NAME).resolve()
    )
    assert settings.agent_bridge_enabled is True
    assert settings.agent_mcp_probe_timeout_s == 5


def test_dependent_state_paths_are_derived_from_custom_state_dir(
    monkeypatch, tmp_path
):
    state_dir = tmp_path / "custom-state"
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(state_dir))
    clear_settings_cache()

    settings = get_settings()
    assert settings.agent_config_dir == app_paths().agent_config_dir.resolve()
    assert (
        settings.audit_log_path
        == (state_dir / AUDIT_LOG_STATE_DIR_NAME / "audit.jsonl").resolve()
    )
    assert (
        settings.agent_auth_dir
        == (state_dir / AGENT_AUTH_STATE_DIR_NAME).resolve()
    )


def test_load_agent_manifest_missing_config(tmp_path):
    manifest = load_agent_manifest(tmp_path)

    assert manifest.status == "missing_config"
    assert manifest.config_path == tmp_path / "config.json"
    assert manifest.data == AgentBridgeManifest()
    assert manifest.errors == []


def test_load_agent_manifest_valid_config(tmp_path):
    config = {
        "version": 1,
        "mcpServers": {
            "github": {
                "integrationId": "github",
                "type": "stdio",
                "command": "github-mcp-server",
                "args": ["stdio"],
                "env": {"GITHUB_TOKEN": "secret"},
            },
            "docs": {
                "integrationId": "docs",
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer secret"},
                "enabled": False,
            },
        },
        "skills": {"enabled": True, "directory": "skills"},
        "dynamicTools": {"mcp": False, "skills": True},
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    manifest = load_agent_manifest(tmp_path)

    assert manifest.status == "loaded"
    assert manifest.data.mcp_servers["github"].command == "github-mcp-server"
    assert manifest.data.mcp_servers["docs"].enabled is False
    assert manifest.data.dynamic_tools.mcp is False
    assert manifest.data.skills.directory == "skills"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/mode contract")
def test_agent_manifest_directory_is_tightened_to_private_mode(tmp_path):
    config_dir = tmp_path / "agent"
    config_dir.mkdir(mode=0o755)
    config_dir.chmod(0o755)
    (config_dir / "config.json").write_text("{}", encoding="utf-8")

    manifest = load_agent_manifest(config_dir)

    assert manifest.status == "loaded"
    assert config_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires privileges on Windows"
)
def test_agent_manifest_rejects_symlink_config_directory(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "config.json").write_text("{}", encoding="utf-8")
    config_dir = tmp_path / "agent"
    config_dir.symlink_to(actual, target_is_directory=True)

    with pytest.raises(OSError, match="not a directory"):
        load_agent_manifest(config_dir)


def test_load_agent_manifest_invalid_json(tmp_path):
    (tmp_path / "config.json").write_text("{not-json", encoding="utf-8")

    manifest = load_agent_manifest(tmp_path)

    assert manifest.status == "invalid_config"
    assert manifest.data == AgentBridgeManifest()
    assert manifest.errors
    assert "JSON" in manifest.errors[0] or "Expecting" in manifest.errors[0]


def test_load_agent_manifest_invalid_encoding(tmp_path):
    (tmp_path / "config.json").write_bytes(b'{"version": 1, "bad": "\xff"}')

    manifest = load_agent_manifest(tmp_path)

    assert manifest.status == "invalid_config"
    assert manifest.data == AgentBridgeManifest()
    assert manifest.errors


def test_load_agent_manifest_invalid_schema_does_not_leak_sensitive_inputs(
    tmp_path,
):
    config = {
        "version": 1,
        "mcpServers": {
            "docs": {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": ["Bearer secret"]},
                "env": {"GITHUB_TOKEN": ["ghp_secret"]},
            }
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    manifest = load_agent_manifest(tmp_path)
    errors = "\n".join(manifest.errors)

    assert manifest.status == "invalid_config"
    assert manifest.errors
    assert "Bearer secret" not in errors
    assert "ghp_secret" not in errors


@pytest.mark.parametrize(
    "server",
    [
        {
            "type": "http",
            "url": "https://oauth.example.test/mcp",
            "auth": {"mode": "oauth"},
        },
        {
            "type": "http",
            "url": "https://secret.example.test/mcp",
            "headers": {"Authorization": {"secret": "token"}},
            "auth": {"mode": "secret"},
        },
        {
            "type": "http",
            "url": "https://legacy.example.test/mcp",
            "headers": {"Authorization": "Bearer legacy-token"},
        },
    ],
)
def test_credential_bearing_manifest_requires_stable_integration_id(
    tmp_path, server
):
    (tmp_path / "config.json").write_text(
        json.dumps({"version": 1, "mcpServers": {"docs": server}}),
        encoding="utf-8",
    )

    manifest = load_agent_manifest(tmp_path)

    assert manifest.status == "invalid_config"
    assert "requires a stable integrationId" in "\n".join(manifest.errors)


def test_non_sensitive_literal_manifest_does_not_require_integration_id(
    tmp_path,
):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://docs.example.test/mcp",
                        "headers": {"X-Mode": "1"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    manifest = load_agent_manifest(tmp_path)

    assert manifest.status == "loaded"
    assert manifest.data.mcp_servers["docs"].integration_id is None


def test_manifest_rejects_duplicate_stable_integration_ids(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "integrationId": "shared",
                        "type": "http",
                        "url": "https://docs.example.test/mcp",
                        "auth": {"mode": "oauth"},
                    },
                    "api": {
                        "integrationId": "shared",
                        "type": "http",
                        "url": "https://api.example.test/mcp",
                        "auth": {"mode": "oauth"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    manifest = load_agent_manifest(tmp_path)

    assert manifest.status == "invalid_config"
    assert "is shared by servers" in "\n".join(manifest.errors)


def test_plain_server_name_may_match_another_servers_integration_id(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "shared": {
                        "type": "http",
                        "url": "https://plain.example.test/mcp",
                        "headers": {"X-Mode": "1"},
                    },
                    "oauth": {
                        "integrationId": "shared",
                        "type": "http",
                        "url": "https://oauth.example.test/mcp",
                        "auth": {"mode": "oauth"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    manifest = load_agent_manifest(tmp_path)

    assert manifest.status == "loaded"
    assert manifest.data.mcp_servers["shared"].integration_id is None
    assert manifest.data.mcp_servers["oauth"].integration_id == "shared"


def test_redact_mapping_hides_secret_values():
    redacted = redact_mapping(
        {
            "GITHUB_TOKEN": "ghp_secret",
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "PRIVATE_KEY": "private secret",
            "AWS_ACCESS_KEY_ID": "AKIASECRET",
            "credentials": {"label": "secret"},
            "normal": "visible",
            "nested": {"password": "secret", "label": "ok"},
            "argv": ["--token=secret", "--api-key=secret", "visible"],
            "split_argv": ["--password", "secret", "visible"],
        }
    )

    assert redacted["GITHUB_TOKEN"] == "<redacted>"
    assert redacted["Authorization"] == "<redacted>"
    assert redacted["Cookie"] == "<redacted>"
    assert redacted["PRIVATE_KEY"] == "<redacted>"
    assert redacted["AWS_ACCESS_KEY_ID"] == "<redacted>"
    assert redacted["credentials"] == "<redacted>"
    assert redacted["normal"] == "visible"
    assert redacted["nested"]["password"] == "<redacted>"
    assert redacted["nested"]["label"] == "ok"
    assert redacted["argv"] == [
        "--token=<redacted>",
        "--api-key=<redacted>",
        "visible",
    ]
    assert redacted["split_argv"] == ["--password", "<redacted>", "visible"]
    assert redact_mapping("--token=secret") == "--token=<redacted>"

    spaced_token = redact_mapping("tool --token secret")
    assert "secret" not in spaced_token
    assert "<redacted>" in spaced_token
    assert redact_mapping(["--password secret", "safe"]) == [
        "--password <redacted>",
        "safe",
    ]


def test_redact_text_hides_quoted_dict_argv_and_url_userinfo_secrets():
    redacted = _redact_text(
        '{"api_key": "super-secret"} '
        "{'token': 'super-secret'} "
        '["--token", "super-secret"] '
        "['--token', 'super-secret'] "
        "https://user:super-secret@example.com/path"
    )

    assert "super-secret" not in redacted
    assert redacted.count("<redacted>") == 5
    assert '"api_key": "<redacted>"' in redacted
    assert "'token': '<redacted>'" in redacted
    assert '["--token", "<redacted>"]' in redacted
    assert "['--token', '<redacted>']" in redacted
    assert "https://user:<redacted>@example.com/path" in redacted


def test_redact_text_hides_realistic_stringified_secrets():
    redacted = _redact_text(
        'env={"GITHUB_TOKEN": "ghp_secret"} '
        '{ "X-API-Key": "super secret with spaces!" } '
        "AWS_SECRET_ACCESS_KEY=abc123 "
        "Authorization: Basic abc123\n"
        "Cookie: session=abc123; refresh=def456\n"
        "password: multi word secret"
    )

    assert "ghp_secret" not in redacted
    assert "super secret with spaces!" not in redacted
    assert "abc123" not in redacted
    assert "def456" not in redacted
    assert "multi word secret" not in redacted
    assert "<redacted>" in redacted


def test_redact_text_hides_standalone_high_confidence_tokens():
    tokens = [
        "ghp_1234567890abcdef1234567890abcdef123456",
        "sk-1234567890abcdef1234567890abcdef",
        "AKIA1234567890ABCDEF",
    ]

    redacted = _redact_text(f"probe failed with {' '.join(tokens)}")

    for token in tokens:
        assert token not in redacted
    assert "<redacted>" in redacted


def test_agent_bridge_manifest_populates_python_field_names():
    manifest = AgentBridgeManifest(
        mcpServers={
            "github": AgentMcpServerConfig(
                type="stdio", command="github-mcp-server"
            )
        },
        dynamicTools=AgentDynamicToolsConfig(mcp=False, skills=True),
    )

    assert manifest.mcp_servers["github"].command == "github-mcp-server"
    assert manifest.dynamic_tools.mcp is False


def test_agent_mcp_manifest_accepts_secret_references_and_oauth_scopes():
    secret_server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://secret.example.test/mcp",
            "headers": {"Authorization": {"secret": "bearer_token"}},
            "auth": {"mode": "secret"},
        }
    )
    oauth_server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://oauth.example.test/mcp",
            "auth": {
                "mode": "oauth",
                "scopes": ["tools.read", "tools.read", "tools.write"],
            },
        }
    )

    authorization = secret_server.headers["Authorization"]
    assert isinstance(authorization, AgentSecretReference)
    assert authorization.secret == "bearer_token"
    assert secret_server.auth.mode == "secret"
    assert oauth_server.auth.mode == "oauth"
    assert oauth_server.auth.scopes == ["tools.read", "tools.write"]


def test_agent_mcp_manifest_rejects_oauth_for_stdio():
    with pytest.raises(ValueError, match="OAuth authentication"):
        AgentMcpServerConfig.model_validate(
            {
                "type": "stdio",
                "command": "example",
                "auth": {"mode": "oauth"},
            }
        )


def test_agent_mcp_manifest_rejects_invalid_secret_reference():
    with pytest.raises(ValueError):
        AgentMcpServerConfig.model_validate(
            {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": {"secret": "bad secret"}},
            }
        )


def test_agent_mcp_manifest_rejects_secret_mode_without_reference():
    with pytest.raises(ValueError, match="requires at least one"):
        AgentMcpServerConfig.model_validate(
            {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "literal"},
                "auth": {"mode": "secret"},
            }
        )


def test_agent_mcp_manifest_rejects_secret_reference_without_secret_mode():
    with pytest.raises(ValueError, match="auth.mode='secret'"):
        AgentMcpServerConfig.model_validate(
            {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": {"secret": "token"}},
            }
        )


def test_agent_mcp_manifest_rejects_blank_or_whitespace_oauth_scope():
    for scope in ["", "tools read"]:
        with pytest.raises(ValueError, match="OAuth scopes"):
            AgentMcpServerConfig.model_validate(
                {
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "auth": {"mode": "oauth", "scopes": [scope]},
                }
            )
