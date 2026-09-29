import os

import pytest

import workgate.config.settings as settings_module
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


_EXPECTED_NONNEGATIVE_NUMERIC_SETTINGS = {
    "ui_terminal_idle_timeout_s",
    "oauth_access_token_ttl_s",
    "oauth_max_pending_codes",
    "oauth_client_ttl_s",
    "oauth_max_dynamic_clients",
    "max_jobs",
    "max_http_request_bytes",
    "file_download_default_max_downloads",
    "file_download_max_file_bytes",
}


def test_runtime_numeric_validation_classifies_every_numeric_setting():
    numeric_settings = {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation in {int, float}
    }
    pydantic_constrained = {
        name
        for name, field in Settings.model_fields.items()
        if any(
            type(metadata).__name__ in {"Ge", "Gt"}
            for metadata in field.metadata
        )
    }
    explicitly_validated = {
        *settings_module._POSITIVE_NUMERIC_SETTINGS,
        *settings_module._NONNEGATIVE_NUMERIC_SETTINGS,
        "port",
        "ui_terminal_max_connections",
    }

    assert set(settings_module._NONNEGATIVE_NUMERIC_SETTINGS) == (
        _EXPECTED_NONNEGATIVE_NUMERIC_SETTINGS
    )
    assert numeric_settings == pydantic_constrained | explicitly_validated


@pytest.mark.parametrize("name", settings_module._POSITIVE_NUMERIC_SETTINGS)
def test_runtime_numeric_settings_reject_zero(name):
    with pytest.raises(ValueError, match=name):
        Settings.model_validate({name: 0})


@pytest.mark.parametrize("name", settings_module._NONNEGATIVE_NUMERIC_SETTINGS)
def test_runtime_numeric_settings_accept_documented_zero(name):
    settings = Settings.model_validate({name: 0})

    assert getattr(settings, name) == 0


@pytest.mark.parametrize("name", settings_module._NONNEGATIVE_NUMERIC_SETTINGS)
def test_runtime_numeric_settings_reject_negative_values(name):
    with pytest.raises(ValueError, match=name):
        Settings.model_validate({name: -1})


@pytest.mark.parametrize("port", (-1, 0, 65_536, 70_000))
def test_port_must_be_in_tcp_range(port):
    with pytest.raises(ValueError, match="port"):
        Settings(port=port)


@pytest.mark.parametrize("port", (1, 65_535))
def test_port_accepts_tcp_range_boundaries(port):
    assert Settings(port=port).port == port


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_tool_timeout_must_be_finite(value):
    with pytest.raises(ValueError, match="tool_timeout_s"):
        Settings(tool_timeout_s=value)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "run_shell_default_timeout_s": 11,
                "run_shell_max_timeout_s": 10,
            },
            "run_shell_max_timeout_s",
        ),
        (
            {
                "file_download_default_ttl_s": 61,
                "file_download_max_ttl_s": 60,
            },
            "file_download_max_ttl_s",
        ),
    ],
)
def test_runtime_numeric_cross_field_limits(overrides, message):
    with pytest.raises(ValueError, match=message):
        Settings(**overrides)


def test_yaml_numeric_validation_uses_settings_model(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("max_http_request_bytes: -1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="max_http_request_bytes"):
        load_settings(config)


def test_environment_numeric_validation_uses_settings_model(monkeypatch):
    monkeypatch.setenv("WORKGATE_MCP_MAX_SESSIONS", "0")

    with pytest.raises(ValueError, match="mcp_max_sessions"):
        load_settings()
