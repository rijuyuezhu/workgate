"""Authlib-backed OAuth service operations."""

import contextlib
import secrets
import time
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urlparse

from authlib.oauth2.rfc6749.errors import (
    InvalidGrantError,
    InvalidRequestError,
    OAuth2Error,
    UnsupportedGrantTypeError,
)
from authlib.oauth2.rfc7636.challenge import (
    CODE_CHALLENGE_PATTERN,
    CODE_VERIFIER_PATTERN,
    compare_s256_code_challenge,
)

from ...audit import audit
from ...config.settings import get_settings
from ..protocol.adapters import LocalOAuthClient
from ..protocol.token_codec import issue_access_token
from .client_store import persist_approved_clients
from .models import AuthCode, OAuthClient
from .requests import (
    AuthorizationRequestInput,
    RegistrationRequest,
    TokenRequestInput,
)
from .scopes import default_scope
from .state import OAuthState, oauth_state
from .urls import issuer_url, normalize_resource, resource_url

LOOPBACK_REDIRECT_HOSTS = {"127.0.0.1", "::1", "localhost"}
BLOCKED_REDIRECT_SCHEMES = {"javascript", "data"}
CLIENT_ID_GENERATION_ATTEMPTS = 8
AUTHORIZATION_CODE_GENERATION_ATTEMPTS = 8
PENDING_CODE_CAPACITY_ERROR = (
    "Too many pending authorization requests; try again later"
)


class OAuthStateCapacityError(OAuth2Error):
    """Reject OAuth state creation when configured in-memory capacity is full."""

    error = "temporarily_unavailable"
    """OAuth error code returned for temporary in-memory capacity exhaustion."""

    status_code = 503
    """HTTP status used when the error is emitted as a JSON OAuth response."""


REGISTRATION_REDIRECT_ERROR = (
    "redirect_uris must be https, loopback http, or custom private-use URIs"
)


@dataclass(frozen=True)
class AuthorizationRequest:
    """Validated authorization-code request data."""

    client: LocalOAuthClient
    """Registered client adapter."""

    client_name: str
    """Client display name."""

    input: AuthorizationRequestInput
    """Validated authorization input."""

    scope: str
    """Granted scope string."""

    resource: str
    """Bound resource identifier."""

    @property
    def client_id(self) -> str:
        """Return the validated client identifier."""
        return self.client.get_client_id()

    @property
    def redirect_uri(self) -> str:
        """Return the validated redirect URI."""
        if self.input.redirect_uri is None:
            raise RuntimeError(
                "Validated authorization request is missing redirect_uri"
            )
        return self.input.redirect_uri

    @property
    def state(self) -> str | None:
        """Return optional client state."""
        return self.input.state


@dataclass(frozen=True)
class AuthorizationFormContext:
    """Display data consumed by the local authorization approval form."""

    params: dict[str, str]
    """Hidden form parameters."""

    client_id: str
    """Client identifier."""

    client_name: str
    """Client display name."""

    redirect_uri: str
    """Redirect URI shown to the user."""

    resource: str
    """Requested resource."""

    scope: str
    """Requested scope string."""


@dataclass(frozen=True)
class AuthorizationResponse:
    """Authorization-code response values for the redirect adapter."""

    redirect_uri: str
    """Redirect URI for the authorization response."""

    query: dict[str, str]
    """Query parameters for the redirect."""

    code: AuthCode
    """Stored authorization code."""


@dataclass(frozen=True)
class TokenResponse:
    """Token endpoint response values after authorization-code exchange."""

    access_token: str
    """Signed bearer token."""

    token_type: str
    """OAuth token type."""

    scope: str
    """Granted scope string."""

    expires_in: int | None
    """Token lifetime in seconds, if configured."""


@dataclass(frozen=True)
class DynamicClientRegistration:
    """Result of a dynamic public-client registration request."""

    client: OAuthClient
    """Newly created or previously matching public client."""

    reused: bool
    """Whether the request reused an existing registration."""


def oauth_error_message(exc: OAuth2Error) -> str:
    """Return a user-facing message for local approval UI errors."""
    return str(exc.description or exc.error or "invalid_request")


def authorization_form_context(
    request_input: AuthorizationRequestInput,
    auth_request: AuthorizationRequest | None = None,
) -> AuthorizationFormContext:
    """Return local approval form display data without exposing stores to HTTP routes."""
    display_input = auth_request.input if auth_request else request_input
    form_params = display_input.to_oauth_params()
    client_id = (
        auth_request.client_id
        if auth_request
        else request_input.client_id or ""
    )
    client_name = auth_request.client_name if auth_request else "Unknown client"
    if auth_request is None and request_input.client_id:
        state = oauth_state()
        state.require_open()
        with state.client_lock:
            state.require_open()
            client_record = state.clients.get(request_input.client_id)
            if client_record and client_record.client_name:
                client_name = client_record.client_name
    return AuthorizationFormContext(
        params=form_params,
        client_id=client_id,
        client_name=client_name,
        redirect_uri=display_input.redirect_uri or "",
        resource=display_input.resource or resource_url(),
        scope=(auth_request.scope if auth_request else display_input.scope)
        or default_scope(),
    )


def _invalid_authorization_request(description: str) -> InvalidRequestError:
    """Create an Authlib invalid_request error with legacy UI text."""
    return InvalidRequestError(description=description)


def _required_param(params: dict[str, str], key: str) -> str:
    """Return a required authorization parameter or raise an OAuth error."""
    value = params.get(key)
    if value:
        return value
    _raise_invalid(f"Missing {key}")


def _raise_invalid(description: str) -> NoReturn:
    """Raise an Authlib invalid_request error while satisfying type checkers."""
    raise _invalid_authorization_request(description)


def _is_private_use_redirect_scheme(parsed_scheme: str, netloc: str) -> bool:
    """Return whether a non-HTTP redirect scheme is private-use style."""
    return "." in parsed_scheme and not netloc


def _is_allowed_redirect_uri(uri: str) -> bool:
    """Accept HTTPS, loopback HTTP, and custom private-use redirect URIs."""
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if not scheme or scheme in BLOCKED_REDIRECT_SCHEMES:
        return False
    if parsed.fragment:
        return False
    if scheme == "https":
        return bool(parsed.netloc)
    if scheme == "http":
        return parsed.hostname in LOOPBACK_REDIRECT_HOSTS
    return _is_private_use_redirect_scheme(scheme, parsed.netloc)


def _new_client_id(state: OAuthState) -> str:
    """Generate a unique client id while the client registry lock is held."""
    for _ in range(CLIENT_ID_GENERATION_ATTEMPTS):
        client_id = "workgate-" + secrets.token_urlsafe(24)
        if client_id not in state.clients:
            return client_id
    raise InvalidRequestError(description="Unable to allocate unique client_id")


def _registration_key(
    client_name: str | None,
    redirect_uris: list[str],
) -> tuple[str | None, tuple[str, ...]]:
    """Return the stable identity for idempotent public-client registration."""
    return client_name, tuple(sorted(set(redirect_uris)))


def _matching_registered_client_locked(
    state: OAuthState,
    client_name: str | None,
    redirect_uris: list[str],
) -> OAuthClient | None:
    """Choose the preferred matching client while the registry lock is held."""
    requested = _registration_key(client_name, redirect_uris)
    matches = [
        client
        for client in state.clients.values()
        if _registration_key(client.client_name, client.redirect_uris)
        == requested
    ]
    if not matches:
        return None
    return min(
        matches,
        key=lambda client: (
            client.approved_at is None,
            client.created_at,
            client.client_id,
        ),
    )


def _client_expired(client: OAuthClient, *, now: int, ttl_s: int) -> bool:
    """Return whether an unapproved client registration is past its TTL."""
    return (
        client.approved_at is None
        and ttl_s > 0
        and now - client.created_at > ttl_s
    )


def _prune_clients(
    *, now: int | None = None, state: OAuthState | None = None
) -> None:
    """Remove expired, unapproved client registrations from memory."""
    current = state or oauth_state()
    current.require_open()
    with current.client_lock:
        current.require_open()
        settings = get_settings()
        if settings.oauth_client_ttl_s <= 0:
            return
        current_time = int(time.time()) if now is None else now
        for client_id, client in list(current.clients.items()):
            if _client_expired(
                client, now=current_time, ttl_s=settings.oauth_client_ttl_s
            ):
                current.clients.pop(client_id, None)


def register_dynamic_client(
    request: RegistrationRequest,
) -> DynamicClientRegistration:
    """Create or reuse one matching dynamic public-client registration."""
    state = oauth_state()
    state.require_open()
    if not request.redirect_uris:
        raise InvalidRequestError(
            description="redirect_uris must be a non-empty list"
        )
    redirect_uris = list(request.redirect_uris)
    if any(not _is_allowed_redirect_uri(uri) for uri in redirect_uris):
        raise InvalidRequestError(description=REGISTRATION_REDIRECT_ERROR)
    with state.client_lock:
        state.require_open()
        _prune_clients(state=state)
        existing = _matching_registered_client_locked(
            state, request.client_name, redirect_uris
        )
        if existing is not None:
            registration = DynamicClientRegistration(
                client=existing,
                reused=True,
            )
        else:
            settings = get_settings()
            max_clients = settings.oauth_max_dynamic_clients
            pending_clients = sum(
                client.approved_at is None for client in state.clients.values()
            )
            if max_clients > 0 and pending_clients >= max_clients:
                raise InvalidRequestError(
                    description="Too many pending OAuth client registrations"
                )

            client_id = _new_client_id(state)
            client = OAuthClient(
                client_id=client_id,
                redirect_uris=redirect_uris,
                client_name=request.client_name,
            )
            state.clients[client_id] = client
            registration = DynamicClientRegistration(
                client=client,
                reused=False,
            )

    client = registration.client
    if registration.reused:
        audit(
            "oauth_client_reused",
            client_id=client.client_id,
            approved=client.approved_at is not None,
            redirect_uri_count=len(set(client.redirect_uris)),
        )
    else:
        audit(
            "oauth_client_registered",
            client_id=client.client_id,
            redirect_uris=client.redirect_uris,
        )
    return registration


def validate_authorization_request(
    request_input: AuthorizationRequestInput,
) -> AuthorizationRequest:
    """Validate authorization request parameters."""
    state = oauth_state()
    state.require_open()
    request_params = request_input.to_oauth_params()
    if request_params.get("response_type") != "code":
        _raise_invalid("Only response_type=code is supported")

    client_id = _required_param(request_params, "client_id")
    redirect_uri = _required_param(request_params, "redirect_uri")
    resource = _required_param(request_params, "resource")

    normalized_resource = normalize_resource(resource)
    if normalized_resource != resource_url():
        _raise_invalid("resource does not match this MCP server")
    with state.client_lock:
        state.require_open()
        _prune_clients(state=state)
        client_record = state.clients.get(client_id)
    if client_record is None:
        _raise_invalid("Unknown client_id")
    client = LocalOAuthClient(client_record)

    if not client.check_response_type(request_params["response_type"]):
        _raise_invalid("Only response_type=code is supported")
    if not client.check_redirect_uri(redirect_uri):
        _raise_invalid("redirect_uri is not registered for this client")

    try:
        scope = client.get_allowed_scope(request_params.get("scope"))
    except ValueError as exc:
        raise _invalid_authorization_request(str(exc)) from exc

    challenge = request_params.get("code_challenge")
    if not challenge:
        _raise_invalid("Missing code_challenge")
    if not CODE_CHALLENGE_PATTERN.fullmatch(challenge):
        _raise_invalid("Invalid code_challenge")
    method = request_params.get("code_challenge_method")
    if not method:
        _raise_invalid("Missing code_challenge_method")
    if method != "S256":
        _raise_invalid("Only code_challenge_method=S256 is supported")

    return AuthorizationRequest(
        client=client,
        client_name=client_record.client_name or "Unknown client",
        input=request_input,
        scope=scope,
        resource=normalized_resource,
    )


def _approve_client(
    client_id: str, *, now: int | None = None, state: OAuthState | None = None
) -> OAuthClient:
    """Persist a client after the local user approves its first authorization."""
    current = state or oauth_state()
    current.require_open()
    with current.client_lock:
        current.require_open()
        client = current.clients.get(client_id)
        if client is None:
            raise RuntimeError("Approved OAuth client is no longer registered")
        if client.approved_at is not None:
            return client

        client.approved_at = max(
            client.created_at,
            int(time.time()) if now is None else now,
        )
        try:
            persist_approved_clients(
                current.clients, state_store=current.state_store
            )
        except OSError:
            client.approved_at = None
            raise
        audit("oauth_client_approved", client_id=client_id)
        return client


def _ensure_pending_code_capacity(state: OAuthState) -> None:
    """Reject new codes without evicting valid pending authorization state."""
    max_codes = get_settings().oauth_max_pending_codes
    pending_codes = len(state.codes)
    if max_codes <= 0 or pending_codes < max_codes:
        return
    with contextlib.suppress(Exception):
        audit(
            "oauth_code_capacity_exhausted",
            pending_codes=pending_codes,
            max_pending_codes=max_codes,
        )
    raise OAuthStateCapacityError(description=PENDING_CODE_CAPACITY_ERROR)


def _new_authorization_code(state: OAuthState) -> str:
    """Generate an authorization code without overwriting live state."""
    for _ in range(AUTHORIZATION_CODE_GENERATION_ATTEMPTS):
        code = secrets.token_urlsafe(32)
        if code not in state.codes:
            return code
    raise InvalidRequestError(
        description="Unable to allocate authorization code"
    )


def issue_authorization_response(
    request: AuthorizationRequest,
) -> AuthorizationResponse:
    """Atomically reserve capacity, approve the client, and store a one-time code."""
    state = oauth_state()
    state.require_open()
    with state.code_lock:
        state.require_open()
        _prune_codes(state=state)
        _ensure_pending_code_capacity(state)
        code = _new_authorization_code(state)
        auth_code = AuthCode(
            code=code,
            client_id=request.client_id,
            redirect_uri=request.redirect_uri,
            scope=request.scope,
            resource=request.resource,
            code_challenge=request.input.code_challenge,
            code_challenge_method=request.input.code_challenge_method,
        )
        _approve_client(request.client_id, state=state)
        state.codes[code] = auth_code

    audit(
        "oauth_code_issued",
        client_id=auth_code.client_id,
        resource=auth_code.resource,
    )
    query = {"code": code, "iss": issuer_url()}
    if request.state:
        query["state"] = request.state
    return AuthorizationResponse(
        redirect_uri=request.redirect_uri,
        query=query,
        code=auth_code,
    )


def _verify_pkce(code_obj: AuthCode, verifier: str | None) -> bool:
    """Validate an S256-only PKCE verifier against stored authorization state."""
    challenge = code_obj.code_challenge
    if code_obj.code_challenge_method != "S256" or not challenge:
        return False
    if not CODE_CHALLENGE_PATTERN.fullmatch(challenge):
        return False
    if not verifier or not CODE_VERIFIER_PATTERN.fullmatch(verifier):
        return False
    return compare_s256_code_challenge(verifier, challenge)


def _auth_code_expired(code_obj: AuthCode, *, now: int, ttl_s: int) -> bool:
    """Return whether an authorization code is past its configured TTL."""
    return now - code_obj.created_at > ttl_s


def _prune_codes(
    *,
    now: int | None = None,
    keep: str | None = None,
    state: OAuthState | None = None,
) -> None:
    """Remove used or expired authorization codes from the in-memory store."""
    current = state or oauth_state()
    current.require_open()
    with current.code_lock:
        current.require_open()
        settings = get_settings()
        current_time = int(time.time()) if now is None else now
        for code, code_obj in list(current.codes.items()):
            if code == keep:
                continue
            if code_obj.used or _auth_code_expired(
                code_obj, now=current_time, ttl_s=settings.oauth_code_ttl_s
            ):
                current.codes.pop(code, None)


def exchange_authorization_code(
    request_input: TokenRequestInput,
) -> TokenResponse:
    """Exchange an authorization code for a bearer token."""
    state = oauth_state()
    state.require_open()
    grant_type = request_input.grant_type or ""
    if grant_type != "authorization_code":
        raise UnsupportedGrantTypeError(grant_type=grant_type)

    resource = request_input.resource or ""
    if not resource:
        raise InvalidRequestError(description="Missing resource")

    code = request_input.code or ""
    client_id = request_input.client_id or ""
    redirect_uri = request_input.redirect_uri or ""
    verifier = request_input.code_verifier

    settings = get_settings()
    with state.code_lock:
        state.require_open()
        _prune_codes(state=state)
        code_obj = state.codes.get(code)
        if not code_obj or code_obj.used:
            raise InvalidGrantError(description="Unknown or used code")

        if _auth_code_expired(
            code_obj, now=int(time.time()), ttl_s=settings.oauth_code_ttl_s
        ):
            raise InvalidGrantError(description="Expired code")
        if (
            code_obj.client_id != client_id
            or code_obj.redirect_uri != redirect_uri
        ):
            raise InvalidGrantError(description="Client or redirect mismatch")
        if normalize_resource(resource) != normalize_resource(
            code_obj.resource
        ):
            raise InvalidGrantError(description="Resource mismatch")
        if not _verify_pkce(code_obj, verifier):
            raise InvalidGrantError(description="PKCE verification failed")

        code_obj.used = True
        state.codes.pop(code, None)
    credential = issue_access_token(
        client_id=client_id, scope=code_obj.scope, resource=code_obj.resource
    )
    audit("oauth_token_issued", client_id=client_id, resource=code_obj.resource)
    expires_in = (
        settings.oauth_access_token_ttl_s
        if settings.oauth_access_token_ttl_s > 0
        else None
    )
    return TokenResponse(
        access_token=credential,
        token_type="Bearer",
        scope=code_obj.scope,
        expires_in=expires_in,
    )
