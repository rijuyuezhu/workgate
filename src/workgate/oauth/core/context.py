"""Request-local OAuth authorization context."""

from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from .scopes import scope_set

type OAuthClaims = Mapping[str, Any]

_CURRENT_OAUTH_CLAIMS: ContextVar[OAuthClaims | None] = ContextVar(
    "workgate_oauth_claims", default=None
)


@dataclass
class MissingOAuthScopeError(Exception):
    """Raised when the current OAuth claims lack a required scope."""

    scope: str
    """First missing scope."""

    def __str__(self) -> str:
        """Return the user-facing error detail."""
        return f"Missing required OAuth scope: {self.scope}"


def bind_oauth_claims(claims: OAuthClaims | None) -> Token[OAuthClaims | None]:
    """Bind bearer claims to the current request context."""
    return _CURRENT_OAUTH_CLAIMS.set(claims)


def reset_oauth_claims(token: Token[OAuthClaims | None]) -> None:
    """Restore the previous bearer-claims binding."""
    _CURRENT_OAUTH_CLAIMS.reset(token)


def current_oauth_claims() -> OAuthClaims | None:
    """Return bearer claims for the current protected request."""
    return _CURRENT_OAUTH_CLAIMS.get()


def require_oauth_scopes(required_scopes: tuple[str, ...]) -> None:
    """Raise if current bearer claims do not include every required scope."""
    claims = current_oauth_claims()
    if claims is None or not required_scopes:
        return
    granted = scope_set(str(claims.get("scope") or ""))
    for required_scope in required_scopes:
        if required_scope not in granted:
            raise MissingOAuthScopeError(required_scope)
