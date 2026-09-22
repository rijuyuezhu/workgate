"""Control-owned half of protected standalone local executor bootstrap."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from ..app_paths import ensure_private_directory
from ..config.settings import Settings
from ..persistence import FileStateStore
from ..protocol.credentials import (
    executor_credential_is_trusted,
    executor_credential_verifier,
    new_executor_credential,
)
from ..protocol.ids import new_executor_id
from ..protocol.standalone import (
    STANDALONE_BOOTSTRAP_ENV,
    STANDALONE_CONTROL_CHILD_ENV,
    STANDALONE_EXECUTOR_NAME_ENV,
    StandaloneExecutorBootstrap,
)
from ..utils.private_files import atomic_write_private_text
from .state import ControlState, ExecutorTrustRecord

_BOOTSTRAP_MAX_BYTES = 16 * 1024
_STANDALONE_PIN_FILE = "oauth-admin-pin"


def prepare_standalone_control_settings(settings: Settings) -> Settings:
    """Generate/reuse the control-owned local OAuth PIN only in standalone."""
    if (
        os.getenv(STANDALONE_CONTROL_CHILD_ENV) != "1"
        or settings.oauth_admin_pin
    ):
        return settings
    root = ensure_private_directory(settings.state_dir)
    path = root / _STANDALONE_PIN_FILE
    try:
        pin = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pin = ""
    if pin:
        if len(pin) < 16:
            raise RuntimeError(
                f"Invalid standalone OAuth admin PIN at {path}; "
                "remove it to regenerate"
            )
    else:
        pin = secrets.token_urlsafe(24)
        atomic_write_private_text(path, pin + "\n")
    return settings.model_copy(update={"oauth_admin_pin": pin})


def _load_existing(path: Path) -> StandaloneExecutorBootstrap | None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    if size > _BOOTSTRAP_MAX_BYTES:
        raise RuntimeError("standalone bootstrap payload is unexpectedly large")
    try:
        return StandaloneExecutorBootstrap.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except ValueError as exc:
        raise RuntimeError("invalid standalone bootstrap payload") from exc


def maybe_write_standalone_bootstrap(settings: Settings) -> bool:
    """Issue/reuse one private bootstrap payload before the control starts serving."""
    raw_path = os.getenv(STANDALONE_BOOTSTRAP_ENV)
    if not raw_path:
        return False
    path = Path(raw_path)
    if not path.is_absolute():
        raise RuntimeError("standalone bootstrap path must be absolute")

    store = FileStateStore(lambda: settings.state_dir)
    state = ControlState(store)
    state.start()
    try:
        existing = _load_existing(path)
        if existing is not None:
            trust = state.snapshot_executors().get(existing.executor_id)
            if trust is None:
                name = os.getenv(STANDALONE_EXECUTOR_NAME_ENV) or "standalone"
                state.put_executor(
                    ExecutorTrustRecord(
                        executor_id=existing.executor_id,
                        name=name[:80],
                        credential_verifier=executor_credential_verifier(
                            existing.credential
                        ),
                        created_at=time.time(),
                    )
                )
                return True
            if not executor_credential_is_trusted(
                existing.credential,
                trust.credential_verifier,
                revoked_at=trust.revoked_at,
            ):
                raise RuntimeError(
                    "standalone bootstrap payload no longer matches control trust"
                )
            return True

        if state.snapshot_executors():
            raise RuntimeError(
                "standalone executor trust already exists but its local profile "
                "and bootstrap payload are missing; owner recovery is required"
            )
        credential = new_executor_credential()
        executor_id = new_executor_id()
        name = os.getenv(STANDALONE_EXECUTOR_NAME_ENV) or "standalone"
        payload = StandaloneExecutorBootstrap(
            control_url=settings.resolved_base_url,
            executor_id=executor_id,
            credential=credential,
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_private_text(
            path,
            json.dumps(payload.model_dump(mode="json"), sort_keys=True) + "\n",
        )
        state.put_executor(
            ExecutorTrustRecord(
                executor_id=executor_id,
                name=name[:80],
                credential_verifier=executor_credential_verifier(credential),
                created_at=time.time(),
            )
        )
        return True
    finally:
        state.close()
