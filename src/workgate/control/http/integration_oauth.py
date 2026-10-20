"""One-shot browser callback bridge for Control-owned MCP SDK OAuth.

The SDK owns discovery, PKCE, state verification and token persistence. This
module only joins an authenticated WebUI request to its public HTTPS callback.
No third-party code, tokens, or client secrets are sent back to the browser.
"""

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from mcp.client.auth import AuthorizationCodeResult
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ...agent_bridge.auth import (
    build_stored_oauth_provider,
    credential_store_key,
    oauth_status,
)
from ...agent_bridge.auth_store import AgentAuthStore
from ...agent_bridge.mcp import AgentMcpClientManager
from ...agent_bridge.state import load_agent_manifest
from ...audit import audit
from ...oauth.core.context import current_oauth_claims, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_EXECUTOR_USE, SCOPE_SHELL_WRITE

_TTL = 300
_MAX_PENDING = 16
_CALLBACK = "/api/ui/integrations/oauth/callback"


class _Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str
    name: str = Field(min_length=1, max_length=128)


class _SessionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=24, max_length=96)


@dataclass
class _Pending:
    owner: str
    name: str
    credential_key: str
    config_digest: bytes
    expires: float
    callback: asyncio.Future[AuthorizationCodeResult]
    ready: asyncio.Future[str]
    task: asyncio.Task[None] | None = None
    csrf_state: str = ""
    response: AuthorizationCodeResult | None = None
    denied: bool = False
    status: str = "preparing"


class BrowserMcpOAuth:
    """Bounded, process-local authorization handshakes (not a credential store)."""

    def __init__(self) -> None:
        self.sessions: dict[str, _Pending] = {}
        self.states: dict[str, str] = {}

    def expire(self) -> None:
        now = time.monotonic()
        # Completed outcomes remain queryable until their original TTL.
        for sid, pending in list(self.sessions.items()):
            if pending.expires <= now:
                self.cancel(sid)

    def cancel(self, sid: str) -> None:
        pending = self.sessions.pop(sid, None)
        if pending is None:
            return
        self.states.pop(pending.csrf_state, None)
        if pending.task is not None and not pending.task.done():
            pending.task.cancel()
        if not pending.callback.done():
            pending.callback.cancel()

    def cancel_credential(self, key: str) -> None:
        """Drop any outstanding browser handshake for an owner-held identity."""
        for sid, pending in list(self.sessions.items()):
            if pending.credential_key == key:
                self.cancel(sid)

    async def begin(self, request: Request, name: str) -> dict[str, object]:
        require_oauth_scopes((SCOPE_EXECUTOR_USE, SCOPE_SHELL_WRITE))
        runtime = request.app.state.control_runtime
        if runtime is None:
            raise HTTPException(503, "Control unavailable")
        self.expire()
        if len(self.sessions) >= _MAX_PENDING:
            raise HTTPException(429, "Too many pending authorizations")

        manifest = load_agent_manifest(
            runtime.config.agent_config_dir, read_only=True
        )
        server = manifest.data.mcp_servers.get(name)
        if (
            server is None
            or server.type != "http"
            or server.auth.mode != "oauth"
        ):
            raise HTTPException(
                400, "Select a configured Control HTTP OAuth MCP"
            )
        if not server.enabled:
            raise HTTPException(409, "Enable this MCP before authorizing it")
        public = urlsplit(runtime.config.resolved_base_url)
        if public.scheme != "https" and not (
            public.scheme == "http"
            and public.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise HTTPException(
                409, "Configure a public HTTPS Control base_url"
            )
        if (
            public.username
            or public.password
            or public.query
            or public.fragment
            or public.path not in {"", "/"}
        ):
            raise HTTPException(409, "Invalid public Control base_url")
        callback_url = runtime.config.resolved_base_url.rstrip("/") + _CALLBACK

        store = AgentAuthStore(runtime.config.agent_auth_dir)
        key = credential_store_key(name, server)
        if oauth_status(store, name, server)["authorized"]:
            raise HTTPException(
                409,
                "OAuth credentials already usable; check status or clear before reauthorizing",
            )
        # A dynamic client registered by the CLI may allow ONLY its ephemeral
        # loopback callback. Never overwrite its client information silently.
        registered = store.get_client_info(key)
        if registered is not None and callback_url not in [
            str(uri) for uri in registered.redirect_uris or []
        ]:
            raise HTTPException(
                409,
                "Existing OAuth client was registered for a different callback; use its original CLI flow",
            )
        for session in self.sessions.values():
            if session.credential_key == key and session.status in {
                "preparing",
                "waiting",
                "awaiting_confirmation",
                "completing",
            }:
                raise HTTPException(
                    409, "Authorization is already in progress for this MCP"
                )
        sid = secrets.token_urlsafe(32)
        loop = asyncio.get_running_loop()
        pending = _Pending(
            owner=_subject(),
            name=name,
            credential_key=key,
            config_digest=hashlib.sha256(
                server.model_dump_json().encode()
            ).digest(),
            expires=time.monotonic() + _TTL,
            callback=loop.create_future(),
            ready=loop.create_future(),
        )
        self.sessions[sid] = pending

        async def show_authorization(url: str) -> None:
            parsed = urlsplit(url)
            # HTTPS and the SDK's validated issuer metadata are required.
            if parsed.scheme != "https" and not (
                parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("OAuth authorization endpoint must use HTTPS")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("Invalid OAuth authorization endpoint")
            metadata = provider.context.oauth_metadata
            expected = (
                str(metadata.authorization_endpoint)
                if metadata and metadata.authorization_endpoint
                else urlsplit(server.url or "")
                ._replace(path="/authorize", query="", fragment="")
                .geturl()
            )
            if (parsed.scheme, parsed.netloc, parsed.path) != (
                urlsplit(expected).scheme,
                urlsplit(expected).netloc,
                urlsplit(expected).path,
            ):
                raise ValueError("Unexpected authorization endpoint")
            query = parse_qs(parsed.query)
            states = query.get("state", [])
            if query.get("redirect_uri") != [callback_url] or query.get(
                "code_challenge_method"
            ) != ["S256"]:
                raise ValueError(
                    "OAuth SDK callback or PKCE parameters were invalid"
                )
            challenges = query.get("code_challenge", [])
            if (
                len(challenges) != 1
                or not 43 <= len(challenges[0]) <= 128
                or any(
                    not (
                        character.isascii()
                        and (character.isalnum() or character in "-_")
                    )
                    for character in challenges[0]
                )
            ):
                raise ValueError(
                    "OAuth SDK PKCE challenge is missing or invalid"
                )
            if len(states) != 1 or not states[0] or states[0] in self.states:
                raise ValueError("OAuth SDK did not provide a unique state")
            pending.csrf_state = states[0]
            pending.status = "waiting"
            self.states[states[0]] = sid
            if not pending.ready.done():
                pending.ready.set_result(url)

        async def wait_callback() -> AuthorizationCodeResult:
            return await pending.callback

        provider = build_stored_oauth_provider(
            store,
            name,
            server,
            redirect_uri=callback_url,
            redirect_handler=show_authorization,
            callback_handler=wait_callback,
        )

        async def run() -> None:
            try:
                manager = AgentMcpClientManager(
                    max(
                        float(runtime.config.agent_mcp_call_timeout_s),
                        _TTL + 20,
                    ),
                    store,
                    oauth_provider_factory=lambda _name, _server: provider,
                    allow_stdio=False,
                )
                await manager.list_tools(name, server)
                # A 2xx token response is not proof the requested scopes were
                # granted. The SDK records omitted scopes as the requested set.
                tokens = store.get_tokens(key)
                granted = set((tokens.scope or "").split()) if tokens else set()
                if not tokens or not set(server.auth.scopes).issubset(granted):
                    store.clear_tokens(key)
                    raise ValueError(
                        "OAuth server did not grant requested scopes"
                    )
                pending.status = "authorized"
                runtime.agent_bridge.invalidate_all_discovery()
                audit(
                    "integration_oauth_completed",
                    owner="control",
                    name=name,
                    ok=True,
                )
            except asyncio.CancelledError:
                pending.status = "cancelled"
                raise
            except Exception:
                pending.status = "denied" if pending.denied else "failed"
                audit(
                    "integration_oauth_completed",
                    owner="control",
                    name=name,
                    ok=False,
                )
            finally:
                self.states.pop(pending.csrf_state, None)
                if not pending.ready.done():
                    pending.ready.set_exception(
                        ValueError("MCP OAuth could not be started")
                    )

        pending.task = asyncio.create_task(run())
        try:
            url = await asyncio.wait_for(
                asyncio.shield(pending.ready), timeout=25
            )
        except TimeoutError, ValueError:
            self.cancel(sid)
            raise HTTPException(
                409, "OAuth discovery or registration failed"
            ) from None
        return {"session_id": sid, "authorization_url": url, "expires_in": _TTL}

    def current(self, name: str) -> dict[str, object] | None:
        """Recover an active browser handshake after the same user reloads the editor."""
        self.expire()
        for sid, pending in self.sessions.items():
            if (
                pending.owner == _subject()
                and pending.name == name
                and pending.status
                in {
                    "preparing",
                    "waiting",
                    "awaiting_confirmation",
                    "completing",
                }
            ):
                url = ""
                if (
                    pending.status == "waiting"
                    and pending.ready.done()
                    and not pending.ready.cancelled()
                ):
                    url = pending.ready.result()
                return {
                    "session_id": sid,
                    "authorization_url": url,
                    "status": pending.status,
                    "expires_in": max(
                        0, int(pending.expires - time.monotonic())
                    ),
                }
        return None

    def status(self, sid: str) -> dict[str, str]:
        self.expire()
        pending = self.sessions.get(sid)
        if pending is None or pending.owner != _subject():
            raise HTTPException(404, "Authorization session not found")
        return {"status": pending.status, "name": pending.name}

    def finish_callback(self, sid: str, config_dir) -> bool:
        """Complete an SDK handshake after a valid, one-use provider callback."""
        pending = self.sessions.get(sid)
        if pending is None or pending.status != "awaiting_confirmation":
            return False
        manifest = load_agent_manifest(config_dir, read_only=True)
        current = manifest.data.mcp_servers.get(pending.name)
        if (
            current is None
            or hashlib.sha256(current.model_dump_json().encode()).digest()
            != pending.config_digest
        ):
            self.cancel(sid)
            return False
        pending.status = "completing"
        if pending.denied:
            pending.callback.set_exception(ValueError("Authorization denied"))
        elif pending.response:
            pending.callback.set_result(pending.response)
        pending.response = None
        return True

    def receive(self, request: Request, config_dir) -> bool:
        self.expire()
        values = request.query_params
        if any(len(values.getlist(key)) != 1 for key in ("state",)) or any(
            len(values.getlist(key)) > 1 for key in ("code", "iss", "error")
        ):
            return False
        state = values.get("state", "")
        sid = self.states.pop(state, None)
        if sid is None:
            return False
        pending = self.sessions.get(sid)
        if (
            pending is None
            or pending.callback.done()
            or pending.expires <= time.monotonic()
        ):
            return False
        pending.csrf_state = ""
        # The callback only records the response. Only the initiating owner
        # can release it through the authenticated/CSRF-checked POST endpoint.
        pending.denied = bool(values.get("error"))
        code = values.get("code", "")
        if not pending.denied and (not code or len(code) > 4096):
            pending.denied = True
        if not pending.denied:
            pending.response = AuthorizationCodeResult(
                code=code, state=state, iss=values.get("iss")
            )
        pending.status = "awaiting_confirmation"
        return self.finish_callback(sid, config_dir)


def _subject() -> str:
    claims = current_oauth_claims()
    return (
        str(claims.get("sub") or claims.get("client_id") or "local-user")
        if claims
        else "local-user"
    )


def browser_mcp_oauth_routes() -> tuple[list[BaseRoute], list[BaseRoute]]:
    """Protected start/status/cancel API; the one-use OAuth callback is public."""

    def runtime(request: Request) -> BrowserMcpOAuth:
        # Keep one broker per HTTP application, not global across test Controls.
        value = getattr(request.app.state, "browser_mcp_oauth", None)
        if value is None:
            value = BrowserMcpOAuth()
            request.app.state.browser_mcp_oauth = value
        return value

    async def start(request: Request) -> Response:
        try:
            body = _Selection.model_validate(await request.json())
        except Exception:
            raise HTTPException(400, "Invalid MCP selection") from None
        if body.owner != "control":
            raise HTTPException(
                400, "Only Control-owned HTTP MCP OAuth is supported"
            )
        result = await runtime(request).begin(request, body.name)
        return JSONResponse(
            {"ok": True, "data": result}, headers={"Cache-Control": "no-store"}
        )

    async def status(request: Request) -> Response:
        require_oauth_scopes((SCOPE_EXECUTOR_USE, SCOPE_SHELL_WRITE))
        sid = request.query_params.get("session_id", "")
        return JSONResponse(
            {"ok": True, "data": runtime(request).status(sid)},
            headers={"Cache-Control": "no-store"},
        )

    async def current(request: Request) -> Response:
        require_oauth_scopes((SCOPE_EXECUTOR_USE, SCOPE_SHELL_WRITE))
        name = request.query_params.get("name", "")
        if not name or len(name) > 128:
            raise HTTPException(400, "Select a configured MCP server")
        return JSONResponse(
            {"ok": True, "data": runtime(request).current(name)},
            headers={"Cache-Control": "no-store"},
        )

    async def cancel(request: Request) -> Response:
        require_oauth_scopes((SCOPE_EXECUTOR_USE, SCOPE_SHELL_WRITE))
        body = _SessionSelection.model_validate(await request.json())
        broker = runtime(request)
        broker.status(body.session_id)  # Owner check first.
        if broker.sessions[body.session_id].status in {
            "completing",
            "authorized",
        }:
            raise HTTPException(
                409, "Authorization already completing; sign out if necessary"
            )
        broker.cancel(body.session_id)
        return JSONResponse(
            {"ok": True, "data": {"status": "cancelled"}},
            headers={"Cache-Control": "no-store"},
        )

    async def callback(request: Request) -> Response:
        # HTTPS URL / Host must agree with the deployed Control callback.
        control = request.app.state.control_runtime
        if control is None:
            return HTMLResponse("Authorization unavailable", status_code=503)
        public = urlsplit(control.config.resolved_base_url)
        if (
            request.headers.get("host", "").lower() != public.netloc.lower()
            or request.url.path != _CALLBACK
            or len(request.scope.get("query_string", b"")) > 8192
            or any(len(value) > 4096 for value in request.query_params.values())
        ):
            return HTMLResponse(
                "Invalid authorization callback", status_code=400
            )
        result = runtime(request).receive(
            request, control.config.agent_config_dir
        )
        return HTMLResponse(
            "Authorization received. Return to WorkGate; status updates automatically."
            if result
            else "Authorization has expired or was already used.",
            status_code=200 if result else 400,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    protected: list[BaseRoute] = [
        Route("/api/ui/integrations/oauth/start", start, methods=["POST"]),
        Route("/api/ui/integrations/oauth/status", status, methods=["GET"]),
        Route("/api/ui/integrations/oauth/current", current, methods=["GET"]),
        Route("/api/ui/integrations/oauth/cancel", cancel, methods=["POST"]),
    ]
    public: list[BaseRoute] = [Route(_CALLBACK, callback, methods=["GET"])]
    return protected, public
