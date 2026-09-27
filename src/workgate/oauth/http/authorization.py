"""OAuth authorization endpoint, local approval form, and code issuance."""

import hmac
import html as html_lib
from functools import lru_cache
from importlib.resources import files
from xml.sax.saxutils import quoteattr

from authlib.oauth2.rfc6749.errors import OAuth2Error
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from ...audit import audit
from ...config.control import get_control_config
from ..core.requests import AuthorizationRequestInput
from ..core.service import (
    AuthorizationFormContext,
    authorization_form_context,
    issue_authorization_response,
    oauth_error_message,
    validate_authorization_request,
)
from .requests import (
    authorization_input_from_mapping,
    parse_authorization_form,
    parse_authorization_query,
)
from .responses import oauth_redirect

_AUTHORIZE_TEMPLATE = "authorize.html"


@lru_cache(maxsize=1)
def _authorize_template() -> str:
    """Read the package HTML template used by the local approval form."""
    return (
        files("workgate.oauth.http")
        .joinpath(_AUTHORIZE_TEMPLATE)
        .read_text(encoding="utf-8")
    )


def _hidden_inputs(params: dict[str, str]) -> str:
    """Preserve validated authorization parameters as hidden fields in the approval form."""

    return "\n".join(
        f'<input type="hidden" name={quoteattr(k)} value={quoteattr(v)} />'
        for k, v in params.items()
    )


def _authorize_form(
    context: AuthorizationFormContext
    | AuthorizationRequestInput
    | dict[str, str],
    error: str | None = None,
) -> HTMLResponse:
    """Render the local approval form used before issuing an authorization code."""
    if isinstance(context, AuthorizationFormContext):
        form_context = context
    elif isinstance(context, AuthorizationRequestInput):
        form_context = authorization_form_context(context)
    else:
        form_context = authorization_form_context(
            authorization_input_from_mapping(context)
        )
    settings = get_control_config()
    error_html = (
        f'<p style="color:#b00020">{html_lib.escape(error)}</p>'
        if error
        else ""
    )
    pin_hint = (
        "Enter WORKGATE_OAUTH_ADMIN_PIN to approve this ChatGPT connector."
    )
    if not settings.oauth_admin_pin:
        pin_hint = "No admin PIN is configured. Set WORKGATE_OAUTH_ADMIN_PIN before OAuth approval can continue."
    html = (
        _authorize_template()
        .replace("{{CLIENT_NAME}}", html_lib.escape(form_context.client_name))
        .replace("{{CLIENT_ID}}", html_lib.escape(form_context.client_id))
        .replace("{{REDIRECT_URI}}", html_lib.escape(form_context.redirect_uri))
        .replace("{{RESOURCE}}", html_lib.escape(form_context.resource))
        .replace("{{SCOPE}}", html_lib.escape(form_context.scope))
        .replace("{{ERROR_HTML}}", error_html)
        .replace("{{HIDDEN_INPUTS}}", _hidden_inputs(form_context.params))
        .replace("{{PIN_HINT}}", html_lib.escape(pin_hint))
    )
    return HTMLResponse(html)


async def authorize_get(request: Request) -> Response:
    """Validate authorization input and render the approval form for the local user."""
    request_input = parse_authorization_query(request)
    try:
        auth_request = validate_authorization_request(request_input)
    except OAuth2Error as exc:
        return _authorize_form(
            authorization_form_context(request_input),
            error=oauth_error_message(exc),
        )
    return _authorize_form(
        authorization_form_context(request_input, auth_request)
    )


async def authorize_post(request: Request) -> Response:
    """Issue an authorization code after form approval and redirect the client back."""
    request_input, submitted_pin = await parse_authorization_form(request)
    try:
        auth_request = validate_authorization_request(request_input)
    except OAuth2Error as exc:
        return _authorize_form(
            authorization_form_context(request_input),
            error=oauth_error_message(exc),
        )

    form_context = authorization_form_context(request_input, auth_request)
    settings = get_control_config()
    expected_pin = settings.oauth_admin_pin
    if not expected_pin:
        audit("oauth_pin_missing", client_id=request_input.client_id)
        return _authorize_form(
            form_context,
            error="Admin PIN is required before OAuth approval can continue",
        )
    if not hmac.compare_digest(submitted_pin, expected_pin):
        audit("oauth_pin_failed", client_id=request_input.client_id)
        return _authorize_form(form_context, error="Invalid admin PIN")

    try:
        authorization_response = issue_authorization_response(auth_request)
    except OAuth2Error as exc:
        return _authorize_form(form_context, error=oauth_error_message(exc))
    return oauth_redirect(
        authorization_response.redirect_uri, authorization_response.query
    )
