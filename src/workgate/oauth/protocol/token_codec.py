"""Local bearer signing and validation helpers."""

import time
from typing import Any

import jwt

from ...config.control import get_control_config
from ..core.security import oauth_signing_secret
from ..core.urls import issuer_url, resource_url


def _jwt_secret() -> str:
    """Return the strong persisted signing key for local bearer credentials."""
    return oauth_signing_secret()


def issue_access_token(
    *, client_id: str, scope: str, resource: str, subject: str = "local-user"
) -> str:
    """Create a signed bearer credential for an approved client, scope, resource, and subject."""
    settings = get_control_config()
    now = int(time.time())
    payload = {
        "iss": issuer_url(),
        "sub": subject,
        "aud": resource,
        "iat": now,
        "client_id": client_id,
        "scope": scope,
    }
    if settings.oauth_access_token_ttl_s > 0:
        payload["exp"] = now + settings.oauth_access_token_ttl_s
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def validate_bearer_token(token: str) -> dict[str, Any]:
    """Decode and validate issuer, audience, resource, and scope claims for incoming bearer credentials."""
    return jwt.decode(
        token,
        _jwt_secret(),
        algorithms=["HS256"],
        audience=resource_url(),
        issuer=issuer_url(),
        options={"require": ["iat", "aud", "iss"]},
    )
