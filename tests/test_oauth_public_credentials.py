import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import pytest

import workgate.control.http.app as http_app
import workgate.control.mcp.app as mcp_app
from workgate.config.settings import Settings, configure_settings
from workgate.oauth.core.security import (
    MIN_OAUTH_SIGNING_SECRET_BYTES,
    OAUTH_SIGNING_SECRET_FILE_NAME,
    oauth_signing_secret,
    validate_public_oauth_configuration,
)


def _settings(
    tmp_path: Path,
    *,
    auth_mode: Literal["none", "oauth"] = "oauth",
    base_url: str | None = "https://shell.example.test",
    pin: str | None = "12345678",
) -> Settings:
    return Settings(
        workspace_root=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        auth_mode=auth_mode,
        base_url=base_url,
        oauth_admin_pin=pin,
    )


@pytest.mark.parametrize(
    "pin",
    [None, "", "        ", "change-me", "change-me-long-random-pin", "1234567"],
)
def test_public_oauth_rejects_missing_placeholder_and_short_pins(tmp_path, pin):
    settings = _settings(tmp_path, pin=pin)

    with pytest.raises(RuntimeError, match="at least 8 characters"):
        validate_public_oauth_configuration(settings)

    assert not (settings.state_dir / OAUTH_SIGNING_SECRET_FILE_NAME).exists()


def test_public_oauth_accepts_eight_character_pin_and_generates_strong_secret(
    tmp_path,
):
    settings = _settings(tmp_path, pin="12345678")

    validate_public_oauth_configuration(settings)

    path = settings.state_dir / OAUTH_SIGNING_SECRET_FILE_NAME
    secret = path.read_text(encoding="utf-8").strip()
    assert len(secret.encode("utf-8")) >= MIN_OAUTH_SIGNING_SECRET_BYTES
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("auth_mode", "base_url"),
    [("none", "https://shell.example.test"), ("oauth", None)],
)
def test_non_public_or_unauthenticated_configuration_skips_credential_gate(
    tmp_path, auth_mode, base_url
):
    settings = _settings(
        tmp_path,
        auth_mode=auth_mode,
        base_url=base_url,
        pin=None,
    )

    validate_public_oauth_configuration(settings)

    assert not (settings.state_dir / OAUTH_SIGNING_SECRET_FILE_NAME).exists()


@pytest.mark.parametrize(
    "secret",
    [
        "x" * (MIN_OAUTH_SIGNING_SECRET_BYTES - 1),
        "change-me-long-random-secret",
    ],
)
def test_weak_persisted_oauth_signing_secret_is_rejected(tmp_path, secret):
    settings = _settings(tmp_path)
    settings.state_dir.mkdir(parents=True)
    path = settings.state_dir / OAUTH_SIGNING_SECRET_FILE_NAME
    path.write_text(secret + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="at least 32 UTF-8 bytes"):
        oauth_signing_secret(settings)

    assert path.read_text(encoding="utf-8").strip() == secret


def test_concurrent_signing_secret_initialization_reuses_one_value(tmp_path):
    settings = _settings(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(
            pool.map(lambda _index: oauth_signing_secret(settings), range(8))
        )

    assert len(set(values)) == 1
    path = settings.state_dir / OAUTH_SIGNING_SECRET_FILE_NAME
    assert path.read_text(encoding="utf-8").strip() == values[0]


@pytest.mark.parametrize("server_module", [http_app, mcp_app])
def test_http_server_entrypoints_reject_weak_public_pin_before_building(
    tmp_path, monkeypatch, server_module
):
    configure_settings(_settings(tmp_path, pin="short"))

    def fail_build(*args, **kwargs):
        raise AssertionError(
            "server app should not be built before OAuth validation"
        )

    build_name = "build_http_app" if server_module is http_app else "build_mcp"
    monkeypatch.setattr(server_module, build_name, fail_build)

    with pytest.raises(RuntimeError, match="at least 8 characters"):
        if server_module is http_app:
            server_module.run_http()
        else:
            server_module.run_mcp()
