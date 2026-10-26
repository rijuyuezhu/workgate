"""Small private, secret-free snapshot for managed Executor connection status."""

import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..persistence import StateStore

type ConnectionPhase = Literal["connected", "reconnecting", "action-required"]
_STATUS_MAX_BYTES = 1024
_STATUS_STALE_S = 90


class ExecutorConnectionStatusStore:
    """Record connection transitions; native service status reads without IPC."""

    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store

    @property
    def path(self) -> Path:
        return self.state_store.layout.executor_dir / "connection.json"

    def record(
        self, phase: ConnectionPhase, error_class: str | None = None
    ) -> None:
        now = time.time()
        with self.state_store.transaction(self.path):
            try:
                old = self.state_store.read_json(
                    self.path, max_bytes=_STATUS_MAX_BYTES
                )
            except ValueError:
                # A damaged diagnostic must not prevent future status reports.
                old = None
            last_success = (
                old.get("last_success_at") if isinstance(old, dict) else None
            )
            self.state_store.write_json(
                self.path,
                {
                    "phase": phase,
                    "updated_at": now,
                    "last_success_at": (
                        now if phase == "connected" else last_success
                    ),
                    # Exception *class* only; never persist messages or bearers.
                    "error_class": None
                    if error_class is None
                    else error_class[:80],
                },
            )

    def describe(self) -> str:
        try:
            # Writes are atomic; a status read needs no cross-process lock.
            data = self.state_store.read_json(
                self.path, max_bytes=_STATUS_MAX_BYTES
            )
        except OSError, ValueError:
            return "unknown (runtime report unavailable)"
        if not isinstance(data, dict):
            return "unknown (no runtime report)"
        phase = data.get("phase")
        updated = data.get("updated_at")
        if (
            phase not in ("connected", "reconnecting", "action-required")
            or not isinstance(updated, (float, int))
            or isinstance(updated, bool)
        ):
            return "stale (no recent runtime report)"
        reason = data.get("error_class")
        if not isinstance(reason, str) or not reason.isidentifier():
            reason = None
        age = time.time() - updated
        if not math.isfinite(age) or age < 0 or age > _STATUS_STALE_S:
            if phase == "action-required":
                return "stale (last report: action-required" + (
                    f": {reason})" if reason else ")"
                )
            return "stale (no recent runtime report)"
        last_success = data.get("last_success_at")
        previous = ""
        if (
            isinstance(last_success, (float, int))
            and not isinstance(last_success, bool)
            and 0 < last_success <= time.time() + 60
        ):
            previous = "; last contact " + datetime.fromtimestamp(
                last_success, UTC
            ).isoformat(timespec="seconds")
        return f"{phase}" + (f" ({reason})" if reason else "") + previous
