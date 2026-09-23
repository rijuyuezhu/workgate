"""Dependency-light protected local bootstrap payload for standalone mode."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .credentials import ExecutorCredential
from .ids import ExecutorId

STANDALONE_BOOTSTRAP_ENV = "WORKGATE_INTERNAL_STANDALONE_BOOTSTRAP_FILE"
STANDALONE_CONTROL_CHILD_ENV = "WORKGATE_INTERNAL_STANDALONE_CONTROL_CHILD"
STANDALONE_CONTROL_READY_NONCE_ENV = (
    "WORKGATE_INTERNAL_STANDALONE_CONTROL_READY_NONCE"
)
STANDALONE_CONTROL_READY_HEADER = "x-workgate-standalone-ready"
STANDALONE_CONTROL_URL_ENV = "WORKGATE_INTERNAL_STANDALONE_CONTROL_URL"
STANDALONE_EXECUTOR_CHILD_ENV = "WORKGATE_INTERNAL_STANDALONE_EXECUTOR_CHILD"
STANDALONE_EXECUTOR_CONFIG_DIR_ENV = (
    "WORKGATE_INTERNAL_STANDALONE_EXECUTOR_CONFIG_DIR"
)
STANDALONE_EXECUTOR_NAME_ENV = "WORKGATE_INTERNAL_STANDALONE_EXECUTOR_NAME"
STANDALONE_EXECUTOR_OWNER_ACTION_FILE_ENV = (
    "WORKGATE_INTERNAL_STANDALONE_EXECUTOR_OWNER_ACTION_FILE"
)
STANDALONE_EXECUTOR_RUNTIME_DIR_ENV = (
    "WORKGATE_INTERNAL_STANDALONE_EXECUTOR_RUNTIME_DIR"
)


class StandaloneExecutorBootstrap(BaseModel):
    """Short-lived launcher payload that becomes a normal executor profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    control_url: str
    executor_id: ExecutorId
    credential: ExecutorCredential
