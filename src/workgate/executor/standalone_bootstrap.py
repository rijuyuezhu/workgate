"""Executor-owned half of protected standalone local bootstrap."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from ..protocol.standalone import (
    STANDALONE_BOOTSTRAP_ENV,
    STANDALONE_CONTROL_URL_ENV,
    STANDALONE_EXECUTOR_CONFIG_DIR_ENV,
    STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV,
    STANDALONE_EXECUTOR_RUNTIME_DIR_ENV,
    StandaloneExecutorBootstrap,
)
from ..utils.private_files import atomic_write_private_text
from .config import ExecutorConfig
from .profile import ExecutorProfile, ExecutorProfileStore

_BOOTSTRAP_MAX_BYTES = 16 * 1024


class StandaloneExecutorBootstrapError(RuntimeError):
    """Protected standalone bootstrap requires owner repair before retry."""


def mark_standalone_executor_owner_action() -> None:
    """Mark one standalone executor exit as requiring owner repair."""
    raw_path = os.getenv(STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV)
    if not raw_path:
        return
    path = Path(raw_path)
    if not path.is_absolute():
        return
    try:
        atomic_write_private_text(path, "owner-action\n")
    except OSError:
        return


def apply_standalone_executor_paths(
    config: ExecutorConfig,
) -> ExecutorConfig:
    """Namespace executor-local config/scratch paths for one standalone deployment."""
    raw_runtime = os.getenv(STANDALONE_EXECUTOR_RUNTIME_DIR_ENV)
    raw_config = os.getenv(STANDALONE_EXECUTOR_CONFIG_DIR_ENV)
    if not raw_runtime and not raw_config:
        return config
    runtime_root = None if not raw_runtime else Path(raw_runtime)
    config_root = None if not raw_config else Path(raw_config)
    if runtime_root is not None and not runtime_root.is_absolute():
        raise RuntimeError("standalone executor runtime path must be absolute")
    if config_root is not None and not config_root.is_absolute():
        raise RuntimeError("standalone executor config path must be absolute")
    return replace(
        config,
        temp_dir=(
            config.temp_dir
            if runtime_root is None
            else (runtime_root / "tmp").resolve(strict=False)
        ),
        agent_config_dir=(
            config.agent_config_dir
            if config_root is None
            else (config_root / "agent").resolve(strict=False)
        ),
    )


def maybe_import_standalone_bootstrap(
    profile_store: ExecutorProfileStore,
) -> bool:
    """Persist a normal executor profile before any authenticated hello."""
    raw_path = os.getenv(STANDALONE_BOOTSTRAP_ENV)
    control_url = os.getenv(STANDALONE_CONTROL_URL_ENV)
    existing = profile_store.load()
    if existing is not None:
        if control_url and existing.control_url != control_url:
            profile_store.save(
                ExecutorProfile.model_validate(
                    {
                        **existing.model_dump(mode="json"),
                        "control_url": control_url,
                    }
                )
            )
        return False
    if not raw_path:
        return False
    path = Path(raw_path)
    if not path.is_absolute():
        raise StandaloneExecutorBootstrapError(
            "standalone bootstrap path must be absolute"
        )
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise StandaloneExecutorBootstrapError(
            "standalone bootstrap payload is not available"
        ) from exc
    if size > _BOOTSTRAP_MAX_BYTES:
        raise StandaloneExecutorBootstrapError(
            "standalone bootstrap payload is unexpectedly large"
        )
    try:
        payload = StandaloneExecutorBootstrap.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        profile = ExecutorProfile(
            control_url=control_url or payload.control_url,
            executor_id=payload.executor_id,
            credential=payload.credential,
        )
    except ValueError as exc:
        raise StandaloneExecutorBootstrapError(
            "invalid standalone bootstrap payload"
        ) from exc
    profile_store.save(profile)
    path.unlink(missing_ok=True)
    return True
