"""Executor bearer generation and verifier helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated

from pydantic import StringConstraints

_CREDENTIAL_BYTES = 32
_VERIFIER_PREFIX = "sha256:"

ExecutorCredential = Annotated[
    str,
    StringConstraints(pattern=r"^wg_exec_[A-Za-z0-9_-]{43,}$", max_length=192),
]
ExecutorCredentialVerifier = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]


def new_executor_credential() -> str:
    """Return a long-lived high-entropy executor bearer credential."""
    return "wg_exec_" + secrets.token_urlsafe(_CREDENTIAL_BYTES)


def executor_credential_verifier(credential: str) -> str:
    """Return the durable verifier stored by control instead of plaintext."""
    digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    return _VERIFIER_PREFIX + digest


def executor_credential_matches(credential: str, verifier: str) -> bool:
    """Return whether one bearer matches a stored verifier."""
    if not verifier.startswith(_VERIFIER_PREFIX):
        return False
    expected = executor_credential_verifier(credential)
    return hmac.compare_digest(expected, verifier)


def executor_credential_is_trusted(
    credential: str,
    verifier: str,
    *,
    revoked_at: float | None,
) -> bool:
    """Authenticate a bearer; inactivity metadata is intentionally irrelevant."""
    return revoked_at is None and executor_credential_matches(
        credential, verifier
    )
