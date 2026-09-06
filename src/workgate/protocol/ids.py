"""Opaque identifiers used by the control/executor protocol."""

from __future__ import annotations

import secrets
from typing import Annotated

from pydantic import StringConstraints

_OPAQUE_ID_BYTES = 16
_DEVICE_CODE_BYTES = 32
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

ExecutorId = Annotated[
    str,
    StringConstraints(pattern=r"^exec_[A-Za-z0-9_-]{22,}$", max_length=128),
]
SessionId = Annotated[
    str,
    StringConstraints(pattern=r"^sess_[A-Za-z0-9_-]{22,}$", max_length=128),
]
CommandId = Annotated[
    str,
    StringConstraints(pattern=r"^cmd_[A-Za-z0-9_-]{22,}$", max_length=128),
]
DeviceCode = Annotated[
    str,
    StringConstraints(pattern=r"^pair_[A-Za-z0-9_-]{43,}$", max_length=160),
]
UserCode = Annotated[
    str,
    StringConstraints(pattern=r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$"),
]


def _new_opaque_id(prefix: str) -> str:
    """Return a URL-safe identifier with at least 128 bits of randomness."""
    return prefix + secrets.token_urlsafe(_OPAQUE_ID_BYTES)


def new_executor_id() -> str:
    """Return a new stable logical executor identifier."""
    return _new_opaque_id("exec_")


def new_session_id() -> str:
    """Return a new shared control/executor session identifier."""
    return _new_opaque_id("sess_")


def new_command_id() -> str:
    """Return a new ordinary-command correlation identifier."""
    return _new_opaque_id("cmd_")


def new_device_code() -> str:
    """Return a high-entropy secret used to poll one pairing attempt."""
    return "pair_" + secrets.token_urlsafe(_DEVICE_CODE_BYTES)


def new_user_code() -> str:
    """Return a short human pairing code with ambiguous glyphs removed."""
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"
