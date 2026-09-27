import os

import pytest

from workgate.config.settings import (
    Settings,
    initialize_runtime_directories,
    load_settings,
)


def test_default_bind_host_is_loopback(monkeypatch):
    monkeypatch.delenv("WORKGATE_HOST", raising=False)

    settings = Settings()

    assert settings.host == "127.0.0.1"


def test_settings_precedence_config_env_cli(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config_workspace = tmp_path / "config-workspace"
    config.write_text(
        f"""
host: 0.0.0.0
port: 1111
mode: http
workspace_root: {config_workspace}
auth_mode: oauth
""".strip()
    )
    monkeypatch.setenv("WORKGATE_PORT", "2222")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")

    settings = load_settings(
        config,
        {"mode": "stdio", "workspace_root": str(tmp_path / "cli-workspace")},
    )

    assert settings.host == "0.0.0.0"
    assert settings.port == 2222
    assert settings.mode == "stdio"
    assert settings.workspace_root == (tmp_path / "cli-workspace").resolve()
    assert settings.auth_mode == "none"


def test_loading_settings_does_not_create_runtime_directories(tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"

    settings = load_settings(
        overrides={
            "workspace_root": str(workspace),
            "state_dir": str(state),
        }
    )

    assert not workspace.exists()
    assert not state.exists()
    assert not settings.audit_log_path.parent.exists()

    initialize_runtime_directories(settings)

    assert workspace.is_dir()
    assert state.is_dir()
    assert settings.audit_log_path.parent.is_dir()
    if os.name != "nt":
        assert state.stat().st_mode & 0o777 == 0o700
        assert settings.audit_log_path.parent.stat().st_mode & 0o777 == 0o700


def test_settings_rejects_non_mapping_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("- not\n- a\n- mapping\n")

    try:
        load_settings(config)
    except ValueError as exc:
        assert "Config file must contain a mapping" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_config_file_rejects_nested_unknown_keys(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
host: 127.0.0.1
auth:
  mode: none
""".strip()
    )
    monkeypatch.delenv("WORKGATE_AUTH_MODE", raising=False)

    with pytest.raises(ValueError, match="Unknown config settings.*auth"):
        load_settings(config)


def test_none_overrides_clear_config_and_env_values(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("base_url: https://example.com\n")
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "test-pin")

    settings = load_settings(
        config,
        {"base_url": None, "oauth_admin_pin": None},
    )

    assert settings.base_url is None
    assert settings.oauth_admin_pin is None


def test_resolved_base_url_prefers_configured_base_url():
    settings = load_settings(overrides={"base_url": "https://example.com/"})

    assert settings.resolved_base_url == "https://example.com"


def test_resolved_base_url_falls_back_to_host_and_port():
    settings = load_settings(
        overrides={"base_url": None, "host": "127.0.0.1", "port": 9999},
    )

    assert settings.resolved_base_url == "http://127.0.0.1:9999"


def test_resolved_base_url_normalizes_wildcard_host():
    settings = load_settings(
        overrides={"base_url": None, "host": "0.0.0.0", "port": 9999},
    )

    assert settings.resolved_base_url == "http://127.0.0.1:9999"


def test_resolved_base_url_brackets_ipv6_host():
    settings = load_settings(
        overrides={"base_url": None, "host": "::1", "port": 9999},
    )

    assert settings.resolved_base_url == "http://[::1]:9999"


def test_audit_payload_limits_must_be_nested():
    for overrides, message in (
        (
            {
                "audit_inline_value_bytes": 2_048,
                "max_audit_payload_bytes": 1_024,
                "max_audit_payload_store_bytes": 4_096,
            },
            "audit_inline_value_bytes",
        ),
        (
            {
                "audit_inline_value_bytes": 256,
                "max_audit_payload_bytes": 8_192,
                "max_audit_payload_store_bytes": 4_096,
            },
            "max_audit_payload_bytes",
        ),
    ):
        try:
            load_settings(overrides=overrides)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(
                "expected nested audit payload limit validation"
            )
