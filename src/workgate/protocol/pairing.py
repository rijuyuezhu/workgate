"""Device-style executor pairing wire models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .credentials import ExecutorCredential
from .ids import DeviceCode, ExecutorId, UserCode


class PairingExecutorMetadata(BaseModel):
    """Small diagnostic metadata shown to the owner during pairing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    platform: str | None = Field(default=None, min_length=1, max_length=128)
    build: str | None = Field(default=None, min_length=1, max_length=128)


class PairStartRequest(BaseModel):
    """Unauthenticated request that starts one bounded pairing attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_name: str | None = Field(
        default=None, min_length=1, max_length=80
    )
    existing_executor_id: ExecutorId | None = None
    metadata: PairingExecutorMetadata = Field(
        default_factory=PairingExecutorMetadata
    )


class PairStartResponse(BaseModel):
    """Information needed by one executor to complete owner approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_code: DeviceCode
    user_code: UserCode
    verification_uri: str = Field(min_length=1, max_length=2048)
    expires_in: int = Field(gt=0, le=24 * 60 * 60)
    poll_interval: int = Field(gt=0, le=60)


class PairDecision(StrEnum):
    """Owner decision for one pending pairing attempt."""

    APPROVE = "approve"
    DENY = "deny"


class PairApprovalRequest(BaseModel):
    """Owner-side decision keyed by the short human code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_code: UserCode
    decision: PairDecision
    replace_executor_id: ExecutorId | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)


class PairPollRequest(BaseModel):
    """Secret-bearing poll request for one pairing attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_code: DeviceCode


class PairPollSuccess(BaseModel):
    """Successful pairing delivery; pending/denied/expired use protocol errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executor_id: ExecutorId
    credential: ExecutorCredential
