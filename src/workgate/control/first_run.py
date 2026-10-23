"""Create a private, durable HTTP Control configuration on first installation."""

import argparse
import os
import secrets
import stat
from pathlib import Path

import yaml

from ..app_paths import app_paths, ensure_private_directory
from ..config.control import CONTROL_SETTING_NAMES, resolve_control_config
from ..config.settings import load_settings
from ..oauth.core.security import (
    oauth_signing_secret,
    validate_oauth_admin_pin,
    validate_public_oauth_configuration,
)
from ..persistence import StateLayout
from ..utils.private_files import write_private_bytes


def initialize_control_config(
    *,
    path: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    base_url: str | None = None,
    state_dir: Path | None = None,
) -> bool:
    """Create config once; subsequent calls validate, never replace owner secrets."""
    if any(key.startswith("WORKGATE_") for key in os.environ):
        raise RuntimeError(
            "save settings to YAML first; WORKGATE_* environment overrides "
            "are not durable for Control services"
        )
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise RuntimeError(f"refusing to use symlinked configuration: {path}")
    created = False
    if not path.exists():
        # A non-loopback bind needs a public OAuth URL for stable callback
        # metadata; the default remains private loopback.
        if host not in {"127.0.0.1", "::1", "localhost"} and not base_url:
            raise ValueError("non-loopback Control requires --base-url")
        from ..config.settings import Settings

        values = {
            "mode": "http",
            "host": host,
            "port": port,
            "auth_mode": "oauth",
            "oauth_admin_pin": secrets.token_urlsafe(24),
        }
        if base_url is not None:
            values["base_url"] = base_url
        if state_dir is not None:
            values["state_dir"] = str(state_dir.expanduser().absolute())
        # Validate the generated values before writing anything sensitive.
        Settings(**values)
        if path.parent == app_paths().config_dir:
            ensure_private_directory(path.parent)
        elif os.name != "nt" and (
            path.parent.is_symlink()
            or not path.parent.is_dir()
            or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
        ):
            raise PermissionError(
                "custom Control configuration requires an existing private (0700) directory"
            )
        try:
            write_private_bytes(
                path,
                yaml.safe_dump(values, sort_keys=False).encode("utf-8"),
                exclusive=True,
            )
            created = True
        except FileExistsError:
            # A concurrent caller created the file; never overwrite it.
            pass
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(
            f"Control configuration is not a regular file: {path}"
        )
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise PermissionError(
            "Control configuration contains secrets and must be private (0600)"
        )
    settings = load_settings(path, setting_names=CONTROL_SETTING_NAMES)
    if settings.mode != "http" or settings.auth_mode != "oauth":
        raise RuntimeError(
            "existing Control configuration must use mode: http and auth_mode: oauth"
        )
    validate_oauth_admin_pin(settings.oauth_admin_pin)
    if (
        settings.host not in {"127.0.0.1", "::1", "localhost"}
        and not settings.base_url
    ):
        raise RuntimeError(
            "existing non-loopback Control config requires base_url"
        )
    ensure_private_directory(settings.state_dir)
    ensure_private_directory(StateLayout(settings.state_dir).locks_dir)
    control = resolve_control_config(settings)
    # Public OAuth validation also initializes the canonical signing key.
    # Loopback-only Control needs the same durable identity but no public URL.
    if control.base_url:
        validate_public_oauth_configuration(control)
    else:
        oauth_signing_secret(control)
    return created


def initialize_control_from_args(args: argparse.Namespace) -> None:
    path = Path(args.config) if args.config else app_paths().config_file
    created = initialize_control_config(
        path=path,
        host=args.host,
        port=args.port,
        base_url=args.base_url,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )
    print(
        f"Control configuration: {'created' if created else 'preserved'} ({path.expanduser().absolute()})"
    )
    print(
        "OAuth admin PIN is stored in the private YAML file; "
        "read it locally for approval, never put it in shell arguments or logs."
    )
    print("OAuth signing identity is stored privately and preserved on re-run.")
    settings = load_settings(path, setting_names=CONTROL_SETTING_NAMES)
    print(f"WebUI: {settings.resolved_base_url.rstrip('/')}{settings.ui_path}")
    print("Next: workgate control install-service")
    print("For custom YAML paths, pass --config with the file path above.")
    print(
        "Then open the Control WebUI and approve any Executor pairing requests."
    )
