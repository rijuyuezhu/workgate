"""HTTP authentication helpers for protected OAuth routes."""

import ipaddress
from typing import Any

from authlib.oauth2.rfc6749.errors import MissingAuthorizationError, OAuth2Error
from fastapi import HTTPException, Request

from ...audit import audit
from ...config.control import ControlConfig, get_control_config
from ..core.urls import protected_resource_metadata_url
from ..protocol.bearer import bearer_resource_protector


def client_host(request: Request) -> str:
    """Return the peer host, or an empty string when absent."""
    return request.client.host if request.client else ""


def _is_loopback_host(value: str) -> bool:
    """Return whether one host name or address identifies loopback."""
    host = value.split("%", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_is_loopback(value: str) -> bool:
    """Return whether one Host-style authority names a loopback endpoint."""
    authority = value.strip()
    if not authority:
        return False

    if authority.startswith("["):
        end = authority.find("]")
        if end <= 1:
            return False
        host = authority[1:end]
        suffix = authority[end + 1 :]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            return False
    else:
        if authority.count(":") > 1:
            return False
        host, separator, port = authority.partition(":")
        if separator and not port.isdigit():
            return False

    return _is_loopback_host(host)


def _peer_is_loopback(request: Request) -> bool:
    """Return whether the transport peer itself is loopback."""
    return _is_loopback_host(client_host(request).strip().strip("[]"))


def _has_forwarding_metadata(request: Request) -> bool:
    """Return whether request headers indicate a forwarding intermediary."""
    for name in request.headers:
        normalized = name.lower()
        if normalized in {"forwarded", "x-real-ip"} or normalized.startswith(
            "x-forwarded-"
        ):
            return True
    return False


def is_direct_localhost_request(request: Request) -> bool:
    """Return whether one request is unambiguously direct loopback traffic."""
    return (
        _peer_is_loopback(request)
        and _host_is_loopback(request.headers.get("host", ""))
        and not _has_forwarding_metadata(request)
    )


def request_authentication_is_bypassed(
    request: Request, settings: ControlConfig | None = None
) -> bool:
    """Return whether configured policy bypasses credentials for this request."""
    resolved = settings or get_control_config()
    return resolved.auth_mode == "none" or (
        resolved.auth_mode == "oauth"
        and resolved.auth_bypass_localhost
        and resolved.mode == "http"
        and is_direct_localhost_request(request)
    )


def bearer_challenge(*, error: str | None = None) -> str:
    """Build the WWW-Authenticate challenge for MCP OAuth clients."""
    parts = [f'resource_metadata="{protected_resource_metadata_url()}"']
    if error:
        parts.append(f'error="{error}"')
    return "Bearer " + ", ".join(parts)


def verify_oauth(request: Request) -> dict[str, Any]:
    """Validate a bearer token and return its claims."""
    try:
        token = bearer_resource_protector().validate_request((), request)
        return token.claims
    except MissingAuthorizationError as exc:
        raise HTTPException(
            status_code=401,
            detail="Missing OAuth bearer token",
            headers={"WWW-Authenticate": bearer_challenge()},
        ) from exc
    except OAuth2Error as exc:
        audit(
            "oauth_auth_failed",
            error=str(exc),
            path=str(request.url.path),
            ip=client_host(request),
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid OAuth bearer token",
            headers={
                "WWW-Authenticate": bearer_challenge(error="invalid_token")
            },
        ) from exc


def verify_request(request: Request) -> dict[str, Any] | None:
    """Verify one HTTP request according to the configured auth mode. It returns the bearer claims if the request is authenticated, or None if not."""
    settings = get_control_config()
    if request_authentication_is_bypassed(request, settings):
        return None
    match settings.auth_mode:
        case "oauth":
            claims = verify_oauth(request)
        case _:
            raise HTTPException(
                status_code=500,
                detail=f"Unsupported auth_mode: {settings.auth_mode}",
            )
    audit(
        "auth_ok",
        subject=claims.get("sub"),
        path=str(request.url.path),
        ip=client_host(request),
    )
    return claims
