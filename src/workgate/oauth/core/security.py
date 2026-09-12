"""OAuth approval-credential and signing-key hardening."""

import contextlib
import secrets

from ...config.control import ControlSettingsView
from ...config.settings import get_settings
from ...persistence import StateLayout
from ...utils.private_files import atomic_write_private_text, private_file_lock

MIN_OAUTH_ADMIN_PIN_CHARS = 8
MIN_OAUTH_SIGNING_SECRET_BYTES = 32
OAUTH_SIGNING_SECRET_FILE_NAME = "oauth-jwt-secret"
_WEAK_APPROVAL_PINS = {
    "",
    "change-me",
    "change-me-long-random-pin",
}
_WEAK_SIGNING_SECRETS = {
    "",
    "change-me",
    "change-me-long-random-secret",
    "workgate-change-me",
}


def oauth_signing_secret(settings: ControlSettingsView | None = None) -> str:
    """Return a strong persisted signing key, generating one under a private lock."""
    settings = settings or get_settings()
    layout = StateLayout(settings.state_dir)
    path = layout.oauth_signing_secret_path
    lock_path = layout.oauth_signing_lock_path
    with private_file_lock(lock_path):
        try:
            with path.open("r", encoding="utf-8") as handle:
                secret = handle.read().strip()
        except FileNotFoundError:
            secret = ""

        if secret:
            if (
                secret.casefold() in _WEAK_SIGNING_SECRETS
                or len(secret.encode("utf-8")) < MIN_OAUTH_SIGNING_SECRET_BYTES
            ):
                raise RuntimeError(
                    f"Persisted OAuth signing secret at {path} must be a non-placeholder value "
                    f"of at least {MIN_OAUTH_SIGNING_SECRET_BYTES} UTF-8 bytes; remove the file "
                    "to generate a new secret."
                )
            with contextlib.suppress(OSError):
                path.chmod(0o600)
            return secret

        secret = secrets.token_urlsafe(48)
        atomic_write_private_text(path, secret + "\n")
        return secret


def validate_public_oauth_configuration(
    settings: ControlSettingsView | None = None,
) -> None:
    """Reject weak approval credentials before serving a configured public OAuth URL."""
    settings = settings or get_settings()
    if settings.auth_mode != "oauth" or not settings.base_url:
        return

    pin = (settings.oauth_admin_pin or "").strip()
    if (
        pin.casefold() in _WEAK_APPROVAL_PINS
        or len(pin) < MIN_OAUTH_ADMIN_PIN_CHARS
    ):
        raise RuntimeError(
            "WORKGATE_OAUTH_ADMIN_PIN must be set to a non-placeholder value "
            f"of at least {MIN_OAUTH_ADMIN_PIN_CHARS} characters when "
            "WORKGATE_BASE_URL is configured for OAuth."
        )

    oauth_signing_secret(settings)
