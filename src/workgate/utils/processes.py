"""Small cross-platform subprocess launch contracts."""

import os
import subprocess
import sys
from typing import Any


def _env_name(*parts: str) -> str:
    return "".join(parts)


_FROZEN_LOADER_ENV_VARS = (
    _env_name("LD", "_", "LIBRARY", "_", "PATH"),
    _env_name("LD", "_", "PRE", "LOAD"),
    _env_name("DYLD", "_", "LIBRARY", "_", "PATH"),
    _env_name("DYLD", "_", "INSERT", "_", "LIBRARIES"),
)


def _restore_or_remove_loader_var(env: dict[str, str], name: str) -> None:
    original_name = f"{name}_ORIG"
    original_value = env.pop(original_name, None)
    if original_value:
        env[name] = original_value
    else:
        env.pop(name, None)


def user_subprocess_env(*, frozen: bool | None = None) -> dict[str, str]:
    """Return a sanitized environment for commands launched on behalf of users."""
    blocked_names = {"PYTHONPATH"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked_names
        and not key.startswith("WORKGATE_")
        and not key.startswith("DOCKER_")
    }
    is_frozen = (
        bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))
        if frozen is None
        else frozen
    )
    if is_frozen:
        for name in _FROZEN_LOADER_ENV_VARS:
            _restore_or_remove_loader_var(env, name)
    return env


def new_process_group_kwargs(
    *,
    windows: bool | None = None,
    windows_creation_flag: int | None = None,
) -> dict[str, Any]:
    """Return asyncio subprocess kwargs for one isolated child process group."""
    use_windows = os.name == "nt" if windows is None else windows
    if not use_windows:
        return {"start_new_session": True}
    creation_flag = (
        int(vars(subprocess)["CREATE_NEW_PROCESS_GROUP"])
        if windows_creation_flag is None
        else windows_creation_flag
    )
    return {"creationflags": creation_flag}
