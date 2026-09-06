"""Private final executor connection profile and single-instance lock."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..persistence import StateStore
from ..protocol.credentials import ExecutorCredential
from ..protocol.ids import ExecutorId
from ..utils.private_files import private_file_lock

_PROFILE_MAX_BYTES = 16 * 1024


class ExecutorProfile(BaseModel):
    """Durable executor-local identity and bearer used across reconnects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    control_url: str = Field(min_length=1, max_length=4096)
    executor_id: ExecutorId
    credential: ExecutorCredential = Field(repr=False)

    @field_validator("control_url")
    @classmethod
    def _validate_control_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("control_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("control_url must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("control_url must not contain query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("control_url must be an origin without a path")
        return normalized


class ExecutorProfileStore:
    """Persist one small owner-private executor profile through StateStore."""

    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store

    def load(self) -> ExecutorProfile | None:
        """Load the durable profile, returning None before pairing completes."""
        path = self.state_store.layout.executor_profile_path
        with self.state_store.transaction(path):
            payload = self.state_store.read_json(
                path, max_bytes=_PROFILE_MAX_BYTES
            )
        if payload is None:
            return None
        try:
            return ExecutorProfile.model_validate(payload)
        except ValueError as exc:
            raise RuntimeError(f"Invalid executor profile: {path}") from exc

    def save(self, profile: ExecutorProfile) -> None:
        """Atomically persist the complete profile before any authenticated hello."""
        path = self.state_store.layout.executor_profile_path
        with self.state_store.transaction(path):
            self.state_store.write_json(path, profile.model_dump(mode="json"))


class ExecutorAlreadyRunningError(RuntimeError):
    """Raised when another process already owns the final executor profile."""


@contextmanager
def executor_run_lock(state_store: StateStore) -> Generator[None]:
    """Hold the profile's cross-platform single-instance lock without waiting."""
    path = state_store.layout.executor_run_lock_path
    try:
        with private_file_lock(path, timeout_s=0):
            yield
    except TimeoutError as exc:
        raise ExecutorAlreadyRunningError(
            "executor profile is already active in another process"
        ) from exc
