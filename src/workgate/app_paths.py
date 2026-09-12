"""Platform-aware filesystem locations for Workgate-owned application data."""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_APP_NAME = "workgate"
_RUNTIME_FALLBACK_NONCE = secrets.token_hex(8)


def _absolute_env_path(value: str | None) -> Path | None:
    """Accept one environment-provided application base only when already absolute."""
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    return path


def _uid_suffix(env: Mapping[str, str] | None = None) -> str:
    """Return a stable per-user suffix for shared temporary roots."""
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    active_env = os.environ if env is None else env
    username = active_env.get("USERNAME") or active_env.get("USER")
    return username or "user"


@dataclass(frozen=True)
class AppPaths:
    """Independent lifetime namespaces owned by one Workgate installation."""

    config_dir: Path
    state_dir: Path
    data_dir: Path
    cache_dir: Path
    runtime_dir: Path

    @property
    def config_file(self) -> Path:
        """Return the default per-user YAML configuration file."""
        return self.config_dir / "config.yaml"

    @property
    def agent_config_dir(self) -> Path:
        """Return the declarative Agent Bridge configuration directory."""
        return self.config_dir / "agent"

    @property
    def ui_runtime_dir(self) -> Path:
        """Return the regenerable native Human UI materialization directory."""
        return self.cache_dir / "ui-runtime"

    @property
    def temp_dir(self) -> Path:
        """Return the safely disposable Workgate scratch directory."""
        return self.runtime_dir / "tmp"


def _xdg_base(env: Mapping[str, str], name: str, fallback: Path) -> Path:
    """Return an absolute XDG base or the documented fallback."""
    return _absolute_env_path(env.get(name)) or fallback


def _is_private_runtime_base(path: Path) -> bool:
    """Return whether a POSIX XDG runtime directory satisfies its security contract."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and info.st_uid != geteuid():
        return False
    if stat.S_IMODE(info.st_mode) != 0o700:
        return False
    return os.access(path, os.W_OK | os.X_OK)


def _runtime_base(env: Mapping[str, str], temp_root: Path) -> Path:
    """Return a usable runtime base or a private-temp fallback location."""
    configured = _absolute_env_path(env.get("XDG_RUNTIME_DIR"))
    if configured is not None and _is_private_runtime_base(configured):
        return configured / _APP_NAME
    return temp_root / (
        f"{_APP_NAME}-{_uid_suffix(env)}-{_RUNTIME_FALLBACK_NONCE}"
    )


def resolve_app_paths(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
    temp_root: Path | None = None,
) -> AppPaths:
    """Resolve Workgate application paths without third-party dependencies."""
    active_env = os.environ if env is None else env
    user_home = (Path.home() if home is None else home).expanduser()
    active_platform = sys.platform if platform is None else platform
    temporary = (
        Path(tempfile.gettempdir()) if temp_root is None else Path(temp_root)
    )

    if active_platform == "darwin":
        support = user_home / "Library" / "Application Support" / _APP_NAME
        return AppPaths(
            config_dir=support / "config",
            state_dir=support / "state",
            data_dir=support / "data",
            cache_dir=user_home / "Library" / "Caches" / _APP_NAME,
            runtime_dir=temporary
            / f"{_APP_NAME}-{_uid_suffix(active_env)}-{_RUNTIME_FALLBACK_NONCE}",
        )

    if active_platform.startswith("win"):
        roaming = _absolute_env_path(active_env.get("APPDATA")) or (
            user_home / "AppData" / "Roaming"
        )
        local = _absolute_env_path(active_env.get("LOCALAPPDATA")) or (
            user_home / "AppData" / "Local"
        )
        return AppPaths(
            config_dir=roaming / _APP_NAME / "config",
            state_dir=local / _APP_NAME / "state",
            data_dir=local / _APP_NAME / "data",
            cache_dir=local / _APP_NAME / "cache",
            runtime_dir=temporary
            / _APP_NAME
            / f"runtime-{_RUNTIME_FALLBACK_NONCE}",
        )

    config_base = _xdg_base(
        active_env, "XDG_CONFIG_HOME", user_home / ".config"
    )
    state_base = _xdg_base(
        active_env, "XDG_STATE_HOME", user_home / ".local" / "state"
    )
    data_base = _xdg_base(
        active_env, "XDG_DATA_HOME", user_home / ".local" / "share"
    )
    cache_base = _xdg_base(active_env, "XDG_CACHE_HOME", user_home / ".cache")
    return AppPaths(
        config_dir=config_base / _APP_NAME,
        state_dir=state_base / _APP_NAME,
        data_dir=data_base / _APP_NAME,
        cache_dir=cache_base / _APP_NAME,
        runtime_dir=_runtime_base(active_env, temporary),
    )


def app_paths() -> AppPaths:
    """Return application paths for the current process environment."""
    return resolve_app_paths()


def ensure_private_directory(path: Path) -> Path:
    """Create or validate a Workgate-owned private directory without following a leaf symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        # Another process may create the leaf after our lstat(). Never trust the
        # winner: always re-inspect it under the same strict policy below.
        with contextlib.suppress(FileExistsError):
            path.mkdir(parents=True, exist_ok=False, mode=0o700)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(
            f"private application directory is not a directory: {path}"
        )
    if os.name != "nt":
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and info.st_uid != geteuid():
            raise PermissionError(
                f"private application directory is not owned by the current user: {path}"
            )
        path.chmod(0o700)
        verified = path.lstat()
        if stat.S_IMODE(verified.st_mode) != 0o700:
            raise PermissionError(
                f"private application directory is not owner-only: {path}"
            )
    return path
