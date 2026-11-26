"""WebUI OAuth-to-cookie session endpoints."""

import jwt
from authlib.oauth2.rfc6749.errors import OAuth2Error
from fastapi import HTTPException
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response

from ...audit import audit
from ...oauth.core.service import exchange_authorization_code
from ...oauth.http.auth import verify_oauth
from ...oauth.http.requests import parse_token_request
from ...oauth.http.responses import oauth_error
from ...oauth.protocol.token_codec import validate_bearer_token
from ..session import (
    UI_CSRF_HEADER,
    UI_SESSION_BINDING_HEADER,
    UI_SESSION_BINDING_PROTOCOL_PREFIX,
    BrowserSessionUnavailable,
    canonical_ui_origin,
    has_valid_ui_csrf_tokens,
    is_valid_ui_origin,
    is_valid_ui_session_binding_token,
    issue_ui_session,
    renew_ui_session,
    revoke_ui_session,
    ui_csrf_cookie_name,
    ui_origin,
    ui_session_cookie_name,
    validate_ui_session,
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _ui_request_origin_candidates(
    connection: HTTPConnection,
) -> tuple[str, str]:
    """Return transport-scheme and alternate-scheme origins for the Host target."""
    scheme = str(connection.scope.get("scheme") or "").lower()
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    if scheme not in {"http", "https"}:
        raise ValueError("Invalid WebUI request scheme")
    authority = connection.headers.get("host", "").strip()
    if not authority or any(character in authority for character in "/?#@\\"):
        raise ValueError("Invalid WebUI request host")
    alternate_scheme = "https" if scheme == "http" else "http"
    return (
        canonical_ui_origin(f"{scheme}://{authority}"),
        canonical_ui_origin(f"{alternate_scheme}://{authority}"),
    )


def ui_request_origin(connection: HTTPConnection) -> str:
    """Return the browser-visible origin for this HTTP or WebSocket target."""
    transport_origin, alternate_origin = _ui_request_origin_candidates(
        connection
    )
    candidates = (transport_origin, alternate_origin)

    configured_origin = ui_origin()
    if any(
        is_valid_ui_origin(configured_origin, candidate)
        for candidate in candidates
    ):
        return configured_origin

    submitted_origin = connection.headers.get("origin", "").strip()
    if submitted_origin and any(
        is_valid_ui_origin(submitted_origin, candidate)
        for candidate in candidates
    ):
        return canonical_ui_origin(submitted_origin)
    return transport_origin


def has_valid_ui_origin(connection: HTTPConnection) -> bool:
    """Return whether the browser origin exactly matches the request target."""
    try:
        expected = ui_request_origin(connection)
    except UnicodeError, ValueError:
        return False
    return is_valid_ui_origin(connection.headers.get("origin", ""), expected)


def ui_session_binding_token(connection: HTTPConnection) -> str:
    """Return the origin-scoped companion token from HTTP or WebSocket input."""
    header_token = connection.headers.get(UI_SESSION_BINDING_HEADER, "").strip()
    if header_token:
        return header_token
    protocols = connection.headers.get("sec-websocket-protocol", "")
    for protocol in protocols.split(","):
        normalized = protocol.strip()
        if normalized.startswith(UI_SESSION_BINDING_PROTOCOL_PREFIX):
            return normalized.removeprefix(UI_SESSION_BINDING_PROTOCOL_PREFIX)
    return ""


def ui_session_claims(connection: HTTPConnection) -> dict[str, object] | None:
    """Return validated WebUI session claims, or None without a cookie."""
    try:
        origin = ui_request_origin(connection)
    except (UnicodeError, ValueError) as exc:
        raise jwt.InvalidTokenError("Invalid WebUI request origin") from exc
    token = connection.cookies.get(ui_session_cookie_name(origin), "").strip()
    if not token:
        return None
    return validate_ui_session(
        token,
        ui_session_binding_token(connection),
        origin,
    )


def has_valid_ui_csrf(
    connection: HTTPConnection, claims: dict[str, object]
) -> bool:
    """Return whether an unsafe cookie-authenticated request passes CSRF checks."""
    method = str(connection.scope.get("method") or "GET").upper()
    if method in _SAFE_METHODS:
        return True
    if not has_valid_ui_origin(connection):
        return False
    try:
        origin = ui_request_origin(connection)
    except UnicodeError, ValueError:
        return False
    return has_valid_ui_csrf_tokens(
        connection.cookies.get(ui_csrf_cookie_name(origin), "").strip(),
        connection.headers.get(UI_CSRF_HEADER, "").strip(),
        claims,
    )


def set_ui_session_cookies(
    response: Response,
    claims: dict[str, object],
    binding_token: str,
    origin: str,
) -> int | None:
    """Attach persistent HttpOnly session and readable CSRF cookies."""
    session_token, csrf_token, max_age = issue_ui_session(
        claims,
        binding_token,
        origin,
    )
    _set_cookie_pair(response, session_token, csrf_token, origin, max_age)
    return max_age


def _set_cookie_pair(
    response: Response,
    session_token: str,
    csrf_token: str,
    origin: str,
    max_age: int | None,
) -> None:
    cookie_options = {
        "path": "/",
        "secure": origin.startswith("https://"),
        "samesite": "strict",
        "max_age": max_age,
    }
    response.set_cookie(
        ui_session_cookie_name(origin),
        session_token,
        httponly=True,
        **cookie_options,
    )
    response.set_cookie(
        ui_csrf_cookie_name(origin),
        csrf_token,
        httponly=False,
        **cookie_options,
    )


def clear_ui_session_cookies(response: Response, origin: str) -> None:
    """Expire both WebUI browser cookies."""
    response.delete_cookie(ui_session_cookie_name(origin), path="/")
    response.delete_cookie(ui_csrf_cookie_name(origin), path="/")


def _json_ok(data: object = None, message: str = "") -> JSONResponse:
    return JSONResponse(
        {"ok": True, "message": message, "data": data},
        headers={"Cache-Control": "no-store"},
    )


def _json_error(detail: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "detail": detail},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _require_origin(request: Request) -> tuple[str, Response | None]:
    try:
        origin = ui_request_origin(request)
    except UnicodeError, ValueError:
        return "", _json_error("Invalid WebUI request origin", status_code=400)
    if is_valid_ui_origin(request.headers.get("origin", ""), origin):
        return origin, None
    return "", _json_error("Invalid WebUI origin", status_code=403)


def _require_binding(request: Request) -> tuple[str, Response | None]:
    binding_token = ui_session_binding_token(request)
    if is_valid_ui_session_binding_token(binding_token):
        return binding_token, None
    return "", _json_error("Invalid WebUI session binding", status_code=400)


async def api_ui_session_oauth(request: Request) -> Response:
    """Exchange a PKCE authorization code into HttpOnly WebUI cookies."""
    origin, error = _require_origin(request)
    if error is not None:
        return error
    binding_token, binding_error = _require_binding(request)
    if binding_error is not None:
        return binding_error
    try:
        token_request = await parse_token_request(request)
        token_response = exchange_authorization_code(token_request)
    except OAuth2Error as exc:
        return oauth_error(exc)

    claims = validate_bearer_token(token_response.access_token)
    try:
        token, csrf_token, expires_in = issue_ui_session(
            claims,
            binding_token,
            origin,
            first_party_redirect=token_request.redirect_uri,
        )
    except OSError, ValueError, RuntimeError:
        return _json_error(
            "Unable to establish browser session", status_code=503
        )
    response = _json_ok({"expires_in": expires_in}, "WebUI session established")
    _set_cookie_pair(response, token, csrf_token, origin, expires_in)
    audit(
        "ui_session_issued",
        client_id=claims.get("client_id"),
        subject=claims.get("sub"),
        expires_in=expires_in,
        origin=origin,
        source="oauth",
    )
    return response


async def api_ui_session_token(request: Request) -> Response:
    """Convert an explicitly supplied OAuth bearer into HttpOnly WebUI cookies."""
    origin, error = _require_origin(request)
    if error is not None:
        return error
    binding_token, binding_error = _require_binding(request)
    if binding_error is not None:
        return binding_error
    try:
        claims = verify_oauth(request)
    except HTTPException as exc:
        return _json_error(str(exc.detail), status_code=exc.status_code)

    response = _json_ok(message="WebUI session established")
    expires_in = set_ui_session_cookies(
        response,
        claims,
        binding_token,
        origin,
    )
    audit(
        "ui_session_issued",
        client_id=claims.get("client_id"),
        subject=claims.get("sub"),
        expires_in=expires_in,
        origin=origin,
        source="bearer",
    )
    return response


async def api_ui_session_renew(request: Request) -> Response:
    """Slide a live first-party cookie on explicit browser activity."""
    origin, error = _require_origin(request)
    if error is not None:
        return error
    try:
        claims = ui_session_claims(request)
        if claims is None:
            return _json_error("WebUI session required", status_code=401)
        if not has_valid_ui_csrf(request, claims):
            return _json_error("WebUI CSRF validation failed", status_code=403)
        renewed = renew_ui_session(claims, origin)
    except BrowserSessionUnavailable:
        return _json_error("Unable to check browser session", status_code=503)
    except jwt.PyJWTError:
        return _json_error("Invalid WebUI session", status_code=401)
    except OSError, ValueError, RuntimeError:
        return _json_error("Unable to renew browser session", status_code=503)
    response = _json_ok({"renewed": renewed is not None})
    if renewed is not None:
        token, max_age = renewed
        _set_cookie_pair(
            response,
            token,
            request.cookies[ui_csrf_cookie_name(origin)],
            origin,
            max_age,
        )
    return response


async def api_ui_session_logout(request: Request) -> Response:
    """Clear WebUI cookies after same-origin and CSRF validation."""
    origin, error = _require_origin(request)
    if error is not None:
        return error
    try:
        claims = ui_session_claims(request)
    except BrowserSessionUnavailable:
        return _json_error("Unable to revoke browser session", status_code=503)
    except jwt.PyJWTError:
        claims = None
    if claims is not None and not has_valid_ui_csrf(request, claims):
        return _json_error("WebUI CSRF validation failed", status_code=403)

    if claims is not None:
        try:
            revoke_ui_session(claims)
        except OSError, ValueError, RuntimeError:
            return _json_error(
                "Unable to revoke browser session", status_code=503
            )
    response = _json_ok(message="WebUI session cleared")
    clear_ui_session_cookies(response, origin)
    audit(
        "ui_session_cleared",
        client_id=claims.get("client_id") if claims else None,
        subject=claims.get("sub") if claims else None,
        origin=origin,
    )
    return response
