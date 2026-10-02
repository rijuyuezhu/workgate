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


def test_server_runtime_controls_resolve_from_yaml_and_env(
    monkeypatch, tmp_path
):
    config = tmp_path / "config.yaml"
    config.write_text(
        "log_level: INFO\nforwarded_allow_ips: 10.0.0.0/8\n",
        encoding="utf-8",
    )

    from_yaml = load_settings(config)
    assert from_yaml.log_level == "info"
    assert from_yaml.forwarded_allow_ips == "10.0.0.0/8"

    monkeypatch.setenv("WORKGATE_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("WORKGATE_FORWARDED_ALLOW_IPS", "127.0.0.1,192.0.2.0/24")
    from_env = load_settings(config)
    assert from_env.log_level == "debug"
    assert from_env.forwarded_allow_ips == "127.0.0.1,192.0.2.0/24"


def test_log_level_validation_is_early_and_case_insensitive():
    assert (
        Settings.model_validate({"log_level": " ERROR "}).log_level == "error"
    )
    with pytest.raises(ValueError, match="log_level"):
        Settings.model_validate({"log_level": "verbose"})


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


@pytest.mark.parametrize(
    "name",
    (
        "mcp_session_idle_timeout_s",
        "mcp_max_sessions",
        "oauth_code_ttl_s",
        "oauth_registration_max_body_bytes",
        "oauth_registration_max_redirect_uris",
        "oauth_registration_max_redirect_uri_chars",
        "oauth_registration_max_client_name_chars",
        "tool_timeout_s",
        "run_shell_default_timeout_s",
        "run_shell_max_timeout_s",
        "max_output_bytes",
        "max_job_log_bytes",
        "max_file_read_bytes",
        "max_view_image_bytes",
        "max_skills",
        "max_skill_related_files",
        "max_skill_scan_entries",
        "max_skill_path_bytes",
        "max_file_write_bytes",
        "max_grep_results",
        "max_directory_entries",
        "max_glob_results",
        "max_tree_entries",
        "max_todos",
        "max_todo_bytes",
        "max_audit_event_bytes",
        "max_transfer_archive_entries",
        "max_transfer_unpacked_bytes",
        "max_concurrent_commands",
        "max_tmux_sessions",
        "file_download_default_ttl_s",
        "file_download_max_ttl_s",
        "agent_mcp_probe_timeout_s",
        "agent_mcp_call_timeout_s",
    ),
)
def test_positive_runtime_settings_reject_zero(name):
    with pytest.raises(ValueError):
        Settings.model_validate({name: 0})


@pytest.mark.parametrize(
    "name",
    (
        "ui_terminal_idle_timeout_s",
        "oauth_access_token_ttl_s",
        "oauth_max_pending_codes",
        "oauth_client_ttl_s",
        "oauth_max_dynamic_clients",
        "max_jobs",
        "agent_session_retention_s",
        "max_http_request_bytes",
        "max_audit_log_bytes",
        "audit_payload_retention_s",
        "max_tmp_files",
        "max_tmp_bytes",
        "file_download_default_max_downloads",
        "file_download_max_file_bytes",
    ),
)
def test_nonnegative_runtime_settings_accept_zero_and_reject_negative(name):
    assert getattr(Settings.model_validate({name: 0}), name) == 0
    with pytest.raises(ValueError):
        Settings.model_validate({name: -1})


@pytest.mark.parametrize("port", (1, 65535))
def test_port_accepts_valid_boundaries(port):
    assert Settings(port=port).port == port


@pytest.mark.parametrize("port", (0, 65536))
def test_port_rejects_values_outside_tcp_range(port):
    with pytest.raises(ValueError):
        Settings(port=port)


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        (
            {
                "run_shell_default_timeout_s": 10,
                "run_shell_max_timeout_s": 9,
            },
            "run_shell_max_timeout_s",
        ),
        (
            {
                "file_download_default_ttl_s": 60,
                "file_download_max_ttl_s": 59,
            },
            "file_download_max_ttl_s",
        ),
    ),
)
def test_related_runtime_limits_must_be_ordered(overrides, message):
    with pytest.raises(ValueError, match=message):
        Settings(**overrides)


def test_numeric_validation_applies_to_yaml_and_environment(
    monkeypatch, tmp_path
):
    config = tmp_path / "config.yaml"
    config.write_text("mcp_max_sessions: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mcp_max_sessions"):
        load_settings(config)

    monkeypatch.setenv("WORKGATE_MCP_MAX_SESSIONS", "0")
    with pytest.raises(ValueError, match="mcp_max_sessions"):
        load_settings(default_config_path=tmp_path / "missing.yaml")


def test_subprocess_env_filters_load_from_yaml_and_env(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "subprocess_env_blocklist:\n"
        "  - YAML_SECRET\n"
        "subprocess_env_blocked_prefixes:\n"
        "  - YAML_PRIVATE_\n",
        encoding="utf-8",
    )

    from_yaml = load_settings(config)
    assert from_yaml.subprocess_env_blocklist == ["YAML_SECRET"]
    assert from_yaml.subprocess_env_blocked_prefixes == ["YAML_PRIVATE_"]

    monkeypatch.setenv(
        "WORKGATE_SUBPROCESS_ENV_BLOCKLIST", " ENV_SECRET ,OTHER_SECRET "
    )
    monkeypatch.setenv(
        "WORKGATE_SUBPROCESS_ENV_BLOCKED_PREFIXES", "ENV_PRIVATE_,TOKEN_"
    )
    from_env = load_settings(config)
    assert from_env.subprocess_env_blocklist == ["ENV_SECRET", "OTHER_SECRET"]
    assert from_env.subprocess_env_blocked_prefixes == [
        "ENV_PRIVATE_",
        "TOKEN_",
    ]


def test_subprocess_env_filters_reject_invalid_entries():
    with pytest.raises(ValueError, match="environment filter entries"):
        Settings.model_validate({"subprocess_env_blocklist": ["BAD-NAME"]})


def test_subprocess_env_filters_accept_empty_values():
    settings = Settings.model_validate(
        {
            "subprocess_env_blocklist": "",
            "subprocess_env_blocked_prefixes": None,
        }
    )

    assert settings.subprocess_env_blocklist == []
    assert settings.subprocess_env_blocked_prefixes == []


def test_subprocess_env_filters_normalize_edge_cases():
    settings = Settings.model_validate(
        {
            "subprocess_env_blocklist": [" SECRET ", "SECRET", ""],
            "subprocess_env_blocked_prefixes": ["PRIVATE_"],
        }
    )

    assert settings.subprocess_env_blocklist == ["SECRET"]
    assert settings.subprocess_env_blocked_prefixes == ["PRIVATE_"]

    with pytest.raises(ValueError, match="must be a list"):
        Settings.model_validate({"subprocess_env_blocklist": 123})
