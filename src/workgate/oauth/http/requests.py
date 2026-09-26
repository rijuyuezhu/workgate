"""Starlette request parsing helpers for OAuth HTTP endpoints.

HTTP routes use this module to extract JSON, query, and form data into typed
``oauth.core`` input models before calling service-layer operations.
"""

import json
from collections.abc import Mapping

from authlib.oauth2.rfc6749.errors import InvalidRequestError
from starlette.datastructures import FormData, QueryParams
from starlette.requests import Request

from ...config.control import get_control_config
from ..core.requests import (
    AuthorizationRequestInput,
    RegistrationRequest,
    TokenRequestInput,
)

_AUTHORIZATION_KNOWN_KEYS = frozenset(
    {
        "response_type",
        "client_id",
        "redirect_uri",
        "resource",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
    }
)


def _string_items(values: Mapping[str, object]) -> dict[str, str]:
    """Return request values as strings, matching Starlette form/query behavior."""
    return {key: str(value) for key, value in values.items()}


def authorization_input_from_mapping(
    values: Mapping[str, object], *, exclude_pin: bool = False
) -> AuthorizationRequestInput:
    """Parse authorization query or form data into a typed service input."""
    data = _string_items(values)
    if exclude_pin:
        data.pop("pin", None)
    extra_params = {
        key: value
        for key, value in data.items()
        if key not in _AUTHORIZATION_KNOWN_KEYS
    }
    return AuthorizationRequestInput(
        response_type=data.get("response_type"),
        client_id=data.get("client_id"),
        redirect_uri=data.get("redirect_uri"),
        resource=data.get("resource"),
        scope=data.get("scope"),
        state=data.get("state"),
        code_challenge=data.get("code_challenge"),
        code_challenge_method=data.get("code_challenge_method"),
        extra_params=extra_params,
    )


def parse_authorization_query(request: Request) -> AuthorizationRequestInput:
    """Parse an authorization GET request into a typed service input."""
    return authorization_input_from_mapping(request.query_params)


async def parse_authorization_form(
    request: Request,
) -> tuple[AuthorizationRequestInput, str]:
    """Parse an authorization approval form and return typed input plus PIN."""
    form = await request.form()
    return authorization_input_from_mapping(form, exclude_pin=True), str(
        form.get("pin") or ""
    )


async def parse_registration_request(request: Request) -> RegistrationRequest:
    """Parse dynamic client registration JSON into a typed service input."""
    settings = get_control_config()
    max_body_bytes = settings.oauth_registration_max_body_bytes
    content_length = request.headers.get("content-length")
    if max_body_bytes > 0 and content_length:
        try:
            if int(content_length) > max_body_bytes:
                raise InvalidRequestError(
                    description=(
                        "Registration payload must be at most "
                        f"{max_body_bytes} bytes"
                    )
                )
        except ValueError:
            pass

    try:
        body_bytes = await request.body()
        if max_body_bytes > 0 and len(body_bytes) > max_body_bytes:
            raise InvalidRequestError(
                description=(
                    "Registration payload must be at most "
                    f"{max_body_bytes} bytes"
                )
            )
        body = json.loads(body_bytes) if body_bytes else {}
    except InvalidRequestError:
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise InvalidRequestError(
            description="Registration payload must be a JSON object"
        )

    raw_redirect_uris = body.get("redirect_uris")
    if not isinstance(raw_redirect_uris, list):
        raise InvalidRequestError(
            description="redirect_uris must be a non-empty list"
        )
    max_redirect_uris = settings.oauth_registration_max_redirect_uris
    if max_redirect_uris > 0 and len(raw_redirect_uris) > max_redirect_uris:
        raise InvalidRequestError(
            description=f"redirect_uris supports at most {max_redirect_uris} entries"
        )
    redirect_uris = tuple(
        value.strip()
        for value in raw_redirect_uris
        if isinstance(value, str) and value.strip()
    )
    if len(redirect_uris) != len(raw_redirect_uris) or not redirect_uris:
        raise InvalidRequestError(
            description="redirect_uris must contain non-empty strings"
        )
    max_redirect_uri_chars = settings.oauth_registration_max_redirect_uri_chars
    if max_redirect_uri_chars > 0 and any(
        len(value) > max_redirect_uri_chars for value in redirect_uris
    ):
        raise InvalidRequestError(
            description=(
                "redirect_uris entries must be at most "
                f"{max_redirect_uri_chars} characters"
            )
        )

    raw_client_name = body.get("client_name")
    client_name = raw_client_name if isinstance(raw_client_name, str) else None
    max_client_name_chars = settings.oauth_registration_max_client_name_chars
    if (
        max_client_name_chars > 0
        and client_name is not None
        and len(client_name) > max_client_name_chars
    ):
        raise InvalidRequestError(
            description=(
                "client_name must be at most "
                f"{max_client_name_chars} characters"
            )
        )
    return RegistrationRequest(
        redirect_uris=redirect_uris,
        client_name=client_name,
    )


async def parse_token_request(request: Request) -> TokenRequestInput:
    """Parse a token endpoint form request into a typed service input."""
    form = await request.form()
    return _token_input_from_form(form)


def _token_input_from_form(form: FormData | QueryParams) -> TokenRequestInput:
    """Convert token endpoint form/query values into a typed service input."""
    data = _string_items(form)
    return TokenRequestInput(
        grant_type=data.get("grant_type"),
        code=data.get("code"),
        client_id=data.get("client_id"),
        redirect_uri=data.get("redirect_uri"),
        resource=data.get("resource"),
        code_verifier=data.get("code_verifier"),
    )
