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
    before = config.read_bytes()
    with pytest.raises(RuntimeError, match="OAUTH_ADMIN_PIN"):
        initialize_control_config(path=config, state_dir=state_dir)
    assert config.read_bytes() == before


def test_placeholder_pin_fails_even_without_public_url(tmp_path):
    config, state_dir = _paths(tmp_path)
    config.write_text(
        "mode: http\nauth_mode: oauth\noauth_admin_pin: change-me-long-random-pin\n"
    )
    with pytest.raises(RuntimeError, match="OAUTH_ADMIN_PIN"):
        initialize_control_config(path=config, state_dir=state_dir)


def test_public_control_requires_base_url(tmp_path):
    config, state_dir = _paths(tmp_path)
    with pytest.raises(ValueError, match="requires --base-url"):
        initialize_control_config(
            path=config, host="0.0.0.0", state_dir=state_dir
        )
    assert not config.exists()


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
