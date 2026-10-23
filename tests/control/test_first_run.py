"""Safe, non-destructive first-time Control setup on native filesystems."""

import os
import stat

import pytest
import yaml

from workgate.control.first_run import (
    initialize_control_config,
    initialize_control_from_args,
)
from workgate.persistence import StateLayout


@pytest.fixture(autouse=True)
def clean_workgate_env(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("WORKGATE_"):
            monkeypatch.delenv(name)


def _paths(tmp_path):
    private = tmp_path / "config"
    private.mkdir(mode=0o700)
    return private / "control.yaml", tmp_path / "state"


def test_initialization_is_private_strong_idempotent_and_secret_safe(
    tmp_path, capsys
):
    config, state_dir = _paths(tmp_path)
    assert initialize_control_config(path=config, state_dir=state_dir)
    values = yaml.safe_load(config.read_text())
    assert values["mode"] == "http"
    assert values["auth_mode"] == "oauth"
    assert values["host"] == "127.0.0.1"
    assert len(values["oauth_admin_pin"]) >= 24
    assert values["state_dir"] == str(state_dir)
    signing = StateLayout(state_dir).oauth_signing_secret_path
    first_key = signing.read_text()
    assert len(first_key.strip()) >= 32
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert stat.S_IMODE(signing.stat().st_mode) == 0o600

    before = config.read_bytes()
    assert not initialize_control_config(path=config, state_dir=state_dir)
    assert config.read_bytes() == before
    assert signing.read_text() == first_key
    assert values["oauth_admin_pin"] not in capsys.readouterr().out


def test_configuration_not_overwritten_and_user_values_preserved(tmp_path):
    config, state_dir = _paths(tmp_path)
    assert initialize_control_config(
        path=config, port=9976, state_dir=state_dir
    )
    existing = config.read_bytes()
    assert not initialize_control_config(
        path=config, port=8765, state_dir=state_dir
    )
    assert config.read_bytes() == existing


def test_insecure_existing_config_fails_without_overwrite(tmp_path):
    config, state_dir = _paths(tmp_path)
    config.write_text("mode: http\nauth_mode: oauth\noauth_admin_pin: '123'\n")
    if os.name != "nt":
        config.chmod(0o600)
    before = config.read_bytes()
    with pytest.raises(RuntimeError, match="OAUTH_ADMIN_PIN"):
        initialize_control_config(path=config, state_dir=state_dir)
    assert config.read_bytes() == before


def test_placeholder_pin_fails_even_without_public_url(tmp_path):
    config, state_dir = _paths(tmp_path)
    config.write_text(
        "mode: http\nauth_mode: oauth\noauth_admin_pin: change-me-long-random-pin\n"
    )
    if os.name != "nt":
        config.chmod(0o600)
    with pytest.raises(RuntimeError, match="OAUTH_ADMIN_PIN"):
        initialize_control_config(path=config, state_dir=state_dir)


def test_public_control_requires_base_url(tmp_path):
    config, state_dir = _paths(tmp_path)
    with pytest.raises(ValueError, match="requires --base-url"):
        initialize_control_config(
            path=config, host="0.0.0.0", state_dir=state_dir
        )
    assert not config.exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "not-a-url",
        "ftp://example.com",
        "https://example.com#fragment",
        "https://example.com?secret=foo",
        "https://user:pass@example.com",
        "https://example.com:badport",
    ],
)
def test_bad_public_base_url_is_rejected_before_creating_secrets(tmp_path, url):
    config, state_dir = _paths(tmp_path)
    with pytest.raises(ValueError, match="base-url"):
        initialize_control_config(
            path=config, host="0.0.0.0", base_url=url, state_dir=state_dir
        )
    assert not config.exists()
    assert not state_dir.exists()


def test_bad_port_does_not_write(tmp_path):
    config, state_dir = _paths(tmp_path)
    with pytest.raises(ValueError, match="port"):
        initialize_control_config(path=config, port=99999, state_dir=state_dir)
    assert not config.exists()


def test_transient_env_rejected(tmp_path, monkeypatch):
    config, state_dir = _paths(tmp_path)
    monkeypatch.setenv("WORKGATE_PORT", "9911")
    with pytest.raises(RuntimeError, match="not durable"):
        initialize_control_config(path=config, state_dir=state_dir)
    assert not config.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="creating symlinks requires Windows developer privileges",
)
def test_rejects_symlinked_config(tmp_path):
    config, state_dir = _paths(tmp_path)
    target = tmp_path / "user-other-file"
    target.write_text("KEEP")
    config.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlinked configuration"):
        initialize_control_config(path=config, state_dir=state_dir)
    assert target.read_text() == "KEEP"


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_rejects_world_readable_existing_secret(tmp_path):
    config, state_dir = _paths(tmp_path)
    config.write_text(
        "mode: http\nauth_mode: oauth\noauth_admin_pin: strongbutpublicpin\n"
    )
    config.chmod(0o644)
    with pytest.raises(PermissionError, match="private"):
        initialize_control_config(path=config, state_dir=state_dir)


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_cannot_chmod_other_users_parent_directory(tmp_path):
    exposed = tmp_path / "project"
    exposed.mkdir(mode=0o755)
    exposed.chmod(0o755)
    with pytest.raises(PermissionError, match="private"):
        initialize_control_config(
            path=exposed / "control.yaml", state_dir=tmp_path / "state"
        )
    assert stat.S_IMODE(exposed.stat().st_mode) == 0o755
    assert not (exposed / "control.yaml").exists()


def test_cli_parsing_and_no_secret_in_printed_output(tmp_path, capsys):
    from workgate.main import _build_parser

    config, state_dir = _paths(tmp_path)
    args = _build_parser().parse_args(
        [
            "control",
            "init-config",
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
            "--port",
            "9123",
        ]
    )
    assert args.handler is initialize_control_from_args
    args.handler(args)
    values = yaml.safe_load(config.read_text())
    printed = capsys.readouterr().out
    assert values["oauth_admin_pin"] not in printed
    assert "Control configuration: created" in printed
    assert "install-service" in printed
    assert "http://127.0.0.1:9123/ui" in printed


@pytest.mark.parametrize("before", [True, False])
def test_first_run_cli_settings_work_before_and_after_subcommand(
    tmp_path, capsys, before
):
    from workgate.main import _build_parser

    config, state_dir = _paths(tmp_path)
    options = [
        "--config",
        str(config),
        "--state-dir",
        str(state_dir),
        "--host",
        "127.0.0.1",
        "--port",
        "9977",
    ]
    command = (
        ["control", *options, "init-config"]
        if before
        else [
            "control",
            "init-config",
            *options,
        ]
    )
    args = _build_parser().parse_args(command)
    args.handler(args)
    assert yaml.safe_load(config.read_text())["port"] == 9977
    assert "http://127.0.0.1:9977/ui" in capsys.readouterr().out


def test_first_run_rejects_transient_parent_settings(tmp_path):
    from workgate.main import _build_parser

    config, state_dir = _paths(tmp_path)
    args = _build_parser().parse_args(
        [
            "control",
            "--oauth-admin-pin",
            "should-not-be-used",
            "init-config",
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
        ]
    )
    with pytest.raises(ValueError, match="not an init-config option"):
        args.handler(args)
    assert not config.exists()
    assert not state_dir.exists()


def test_fresh_default_config_home_requires_no_existing_xdg_dir(
    tmp_path, monkeypatch
):
    from workgate.app_paths import app_paths

    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path / "fresh" / "Roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "fresh" / "Local"))
    else:
        monkeypatch.setenv(
            "XDG_CONFIG_HOME", str(tmp_path / "fresh" / ".config")
        )
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "fresh" / ".state"))
    from workgate.control.first_run import initialize_control_config

    path = app_paths().config_file
    assert not path.parent.exists()
    assert initialize_control_config(path=path)
    assert path.is_file()
    assert yaml.safe_load(path.read_text())["mode"] == "http"
    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_existing_config_in_newly_exposed_custom_parent_is_rejected(tmp_path):
    config, state_dir = _paths(tmp_path)
    initialize_control_config(path=config, state_dir=state_dir)
    before = config.read_bytes()
    config.parent.chmod(0o777)
    with pytest.raises(PermissionError, match="private"):
        initialize_control_config(path=config, state_dir=state_dir)
    assert config.read_bytes() == before


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX symlink creation requires no privileges"
)
def test_symlinked_default_config_directory_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    from workgate.app_paths import app_paths

    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    default = app_paths().config_dir
    default.parent.mkdir(parents=True, exist_ok=True)
    default.symlink_to(actual, target_is_directory=True)
    with pytest.raises(PermissionError, match="symlink"):
        initialize_control_config(path=app_paths().config_file)
    assert not (actual / "config.yaml").exists()


def test_disabled_webui_is_not_advertised(tmp_path, capsys):
    from workgate.main import _build_parser

    config, state_dir = _paths(tmp_path)
    assert initialize_control_config(path=config, state_dir=state_dir)
    values = yaml.safe_load(config.read_text())
    values["ui_enabled"] = False
    config.write_text(yaml.safe_dump(values))
    if os.name != "nt":
        config.chmod(0o600)
    args = _build_parser().parse_args(
        [
            "control",
            "init-config",
            "--config",
            str(config),
        ]
    )
    args.handler(args)
    assert "WebUI is disabled" in capsys.readouterr().out
