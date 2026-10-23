"""Create a private, durable HTTP Control configuration on first installation."""

import argparse
import os
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..app_paths import app_paths, ensure_private_directory
from ..config.control import CONTROL_SETTING_NAMES, resolve_control_config
from ..config.settings import load_settings
from ..config.surface import CLI_UNSET
from ..oauth.core.security import (
    oauth_signing_secret,
    validate_oauth_admin_pin,
    validate_public_oauth_configuration,
)
from ..persistence import StateLayout
from ..utils.private_files import write_private_bytes


def _check_base_url(value: str | None) -> None:
    if value is None:
        return
    try:
        url = urlsplit(value)
        valid = (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and url.port != 0
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "--base-url must be an HTTP(S) origin/URL without credentials, query or fragment"
        )


def _check_private_config_parent(path: Path) -> None:
    """Validate the config directory without changing unrelated parents."""
    if path.parent.is_symlink() or (
        os.name == "nt" and path.parent.is_junction()
    ):
        raise PermissionError(
            "Control config directory must not be a symlink or junction"
        )
    if path.parent == app_paths().config_dir:
        # The XDG or AppData base may not exist on a fresh install.
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        ensure_private_directory(path.parent)
    elif os.name != "nt" and (
        not path.parent.is_dir()
        or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
    ):
        raise PermissionError(
            "custom Control configuration requires an existing private (0700) directory"
        )


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
        _check_base_url(base_url)
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
        _check_private_config_parent(path)
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
    if not created:
        _check_private_config_parent(path)
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
    _check_base_url(settings.base_url)
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
    allowed = {"host", "port", "base_url", "state_dir"}
    for name in CONTROL_SETTING_NAMES - allowed:
        if getattr(args, name, CLI_UNSET) is not CLI_UNSET:
            raise ValueError(
                f"--{name.replace('_', '-')} is not an init-config option; "
                "edit the durable YAML after initialization"
            )

    path = Path(args.config) if args.config else app_paths().config_file
    host = args.host if isinstance(args.host, str) else "127.0.0.1"
    port = args.port if isinstance(args.port, int) else 8765
    base_url = args.base_url if isinstance(args.base_url, str) else None
    state_dir = (
        args.state_dir if isinstance(args.state_dir, str | Path) else None
    )
    created = initialize_control_config(
        path=path,
        host=host,
        port=port,
        base_url=base_url,
        state_dir=Path(state_dir) if state_dir is not None else None,
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
    if settings.ui_enabled:
        print(
            f"WebUI: {settings.resolved_base_url.rstrip('/')}{settings.ui_path}"
        )
    else:
        print("WebUI is disabled in the existing Control YAML")
    print("Next: workgate control install-service")
    print("For custom YAML paths, pass --config with the file path above.")
    print(
        "Then open the Control WebUI and approve any Executor pairing requests."
    )
