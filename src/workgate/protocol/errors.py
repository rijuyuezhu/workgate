"""Small shared error vocabulary for executor protocol v1."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ProtocolErrorCode(StrEnum):
    """Stable product-oriented protocol errors shared across the boundary."""

    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    UNAUTHORIZED_EXECUTOR = "unauthorized_executor"
    EXECUTOR_REVOKED = "executor_revoked"
    PAIRING_REQUIRED = "pairing_required"
    PAIRING_PENDING = "pairing_pending"
    PAIRING_DENIED = "pairing_denied"
    PAIRING_EXPIRED = "pairing_expired"
    PAIRING_CAPACITY_EXHAUSTED = "pairing_capacity_exhausted"
    UNKNOWN_COMMAND = "unknown_command"
    EXECUTOR_OVERLOADED = "executor_overloaded"
    OPERATION_UNSUPPORTED = "operation_unsupported"
    SESSION_NOT_FOUND = "session_not_found"
    EXECUTOR_OFFLINE = "executor_offline"


class ProtocolError(BaseModel):
    """One bounded machine-readable protocol failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ProtocolErrorCode
    message: str = Field(min_length=1, max_length=1000)


class ProtocolErrorResponse(BaseModel):
    """HTTP/JSON error envelope used by executor protocol endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ProtocolError
