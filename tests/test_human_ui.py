import base64
import hashlib
import html
import json
import os
import re
import stat
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import workgate.ui.http.routes as human_ui_module
from workgate.config.settings import Settings, clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.core.scopes import default_scope
from workgate.oauth.core.state import (
    OAuthState,
    configure_oauth_state,
    oauth_state,
)
from workgate.oauth.protocol.token_codec import (
    issue_access_token,
    validate_bearer_token,
)
from workgate.ui.security import (
    UI_LOCAL_TOKEN_HEADER,
    get_or_create_ui_local_token,
)
from workgate.ui.session import (
    UI_CSRF_HEADER,
    UI_SESSION_BINDING_HEADER,
    UI_SESSION_BINDING_PROTOCOL_PREFIX,
    UI_SESSION_BINDING_STORAGE_KEY,
    UI_SESSION_ESTABLISHED_STORAGE_KEY,
    UI_SESSION_UNBOUNDED_SOURCE_TTL_S,
    canonical_ui_origin,
    is_valid_ui_origin,
    issue_ui_session,
    ui_csrf_cookie_name,
    ui_session_cookie_name,
    validate_ui_session,
)

UI_SESSION_BINDING = "b" * 43


@pytest.fixture(autouse=True)
def _reset_human_ui_state(tmp_path):
    state = OAuthState(tmp_path / ".oauth-state")
    previous = configure_oauth_state(state)
    clear_settings_cache()
    try:
        yield state
    finally:
        configure_oauth_state(previous)
        clear_settings_cache()


def _configure_ui(monkeypatch, tmp_path, *, auth_mode="none", **values):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_UI_TUI_COMMAND", "test-opentui")
    for name, value in values.items():
        monkeypatch.setenv(f"WORKGATE_{name.upper()}", str(value).lower())
    clear_settings_cache()


def test_ui_path_normalization_and_reserved_paths():
    assert Settings(ui_path="/console/").ui_path == "/console"
    assert Settings(ui_path="//human//ui//").ui_path == "/human/ui"

    for value in (
        "ui",
        "/",
        "/../ui",
        "/api",
        "/api/custom",
        "/ui?x=1",
        '/ui"><script>',
        "/控制台",
    ):
        with pytest.raises(ValidationError):
            Settings(ui_path=value)


def test_human_ui_shell_is_public_but_api_requires_oauth(monkeypatch, tmp_path):
    _configure_ui(monkeypatch, tmp_path, auth_mode="oauth")
    client = TestClient(build_http_app(), client=("203.0.113.10", 50000))

    index = client.get("/ui")
    assert index.status_code == 200
    assert 'class="app-shell"' in index.text
    assert 'class="app-sidebar"' in index.text
    assert 'data-view="overview"' in index.text
    assert 'data-view="sessions"' in index.text
    assert 'data-view="audit"' in index.text
    assert 'data-app-view="overview"' in index.text
    assert 'id="dashboard-panel"' in index.text
    assert 'id="dashboard-machine"' in index.text
    assert 'id="dashboard-cpu-trend"' in index.text
    assert 'id="dashboard-alerts"' in index.text
    assert 'id="dashboard-activity"' in index.text
    assert 'id="remotes-panel"' in index.text
    assert 'id="remote-invite-dialog"' in index.text
    assert 'id="remote-invite-result-dialog"' in index.text
    assert 'id="remote-reconnect-copy"' in index.text
    assert 'id="remote-detail-reconnect"' in index.text
    assert 'id="remote-rename-dialog"' in index.text
    assert 'id="remote-revoke-dialog"' in index.text
    assert 'id="terminal-machine"' in index.text
    assert 'id="terminal-xterm"' in index.text
    assert 'id="terminal-latest"' in index.text
    assert 'id="terminal-keyboard"' in index.text
    assert 'data-terminal-key="ctrl-c"' in index.text

    assert "assets/xterm.css" in index.text
    assert "assets/xterm_bundle.js" in index.text
    assert "assets/terminal_renderer.js" in index.text
    assert "assets/opentui_console.js" in index.text
    assert 'id="opentui-panel"' in index.text
    assert 'id="opentui-terminal"' in index.text
    assert 'id="file-panel"' in index.text
    assert 'id="file-machine"' in index.text
    assert 'id="file-editor-form"' in index.text
    assert 'id="file-copy"' in index.text
    assert 'id="file-move"' in index.text
    assert 'id="file-rename"' in index.text
    assert "Migration status" not in index.text
    assert "Human UI foundation active" not in index.text
    assert "__WORKGATE_UI_PATH__" not in index.text
    assert "__WORKGATE_UI_ASSET_REV__" not in index.text
    asset_revision = re.search(r"assets/web\.js\?v=([0-9a-f]{16})", index.text)
    assert asset_revision is not None
    assert f"assetRevision&quot;:&quot;{asset_revision.group(1)}" in index.text
    assert index.headers["cache-control"] == "no-store"
    csp = index.headers["content-security-policy"]
    assert "script-src 'self' 'wasm-unsafe-eval'" in csp
    assert "'unsafe-eval'" not in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp
    assert client.get("/ui/callback?code=example").status_code == 200
    renderer = client.get("/ui/assets/terminal_renderer.js")
    assert renderer.status_code == 200
    assert renderer.headers["cache-control"] == "no-cache"
    assert renderer.headers["x-content-type-options"] == "nosniff"
    assert "WorkgateTerminalRenderer" in renderer.text
    assert "MAX_RUNS = 10_000" in renderer.text
    assert "createTextNode" in renderer.text
    assert "innerHTML" not in renderer.text

    xterm_bundle = client.get("/ui/assets/xterm_bundle.js")
    assert xterm_bundle.status_code == 200
    assert xterm_bundle.headers["x-content-type-options"] == "nosniff"
    assert "WorkgateXterm" in xterm_bundle.text
    assert "createImageAddon" in xterm_bundle.text
    assert "WEB_IMAGE_ADDON_OPTIONS" in xterm_bundle.text
    assert "sourceMappingURL" not in xterm_bundle.text
    assert client.get("/ui/assets/xterm.css").status_code == 200
    web_css = client.get("/ui/assets/web.css")
    assert web_css.status_code == 200
    assert "scrollbar-width: none !important" in web_css.text
    assert ".terminal-xterm .xterm-viewport::-webkit-scrollbar" in web_css.text
    license_text = client.get("/ui/assets/xterm.LICENSE.txt")
    assert license_text.status_code == 200
    assert "Permission is hereby granted" in license_text.text
    assert "@xterm/xterm 5.5.0" in license_text.text
    assert "@xterm/addon-fit 0.10.0" in license_text.text
    assert "@xterm/addon-image 0.8.0" in license_text.text
    opentui_script = client.get("/ui/assets/opentui_console.js")
    assert opentui_script.status_code == 200
    assert opentui_script.headers["x-content-type-options"] == "nosniff"
    assert "createImageAddon" in opentui_script.text
    assert opentui_script.text.index("loadAddon(api.createImageAddon())") < (
        opentui_script.text.index("fitAddon = new api.FitAddon()")
    )
    script = client.get("/ui/assets/web.js")
    assert script.status_code == 200
    assert script.headers["x-content-type-options"] == "nosniff"
    assert "await Promise.all([" in script.text
    assert 'import(assetUrl("dashboard.js"))' in script.text
    assert 'import(assetUrl("remotes.js"))' in script.text
    assert 'import(assetUrl("audit_view.js"))' in script.text
    assert 'import(assetUrl("audit.js"))' in script.text
    assert 'import(assetUrl("sessions.js"))' in script.text
    assert 'import(assetUrl("terminal.js"))' in script.text
    assert 'import(assetUrl("files.js"))' in script.text
    assert "?v=${assetRevision}" in script.text
    dashboard_script = client.get("/ui/assets/dashboard.js")
    assert dashboard_script.status_code == 200
    assert dashboard_script.headers["x-content-type-options"] == "nosniff"
    assert "export function createDashboardController" in dashboard_script.text
    remotes_script = client.get("/ui/assets/remotes.js")
    assert remotes_script.status_code == 200
    assert remotes_script.headers["x-content-type-options"] == "nosniff"
    assert "export function createRemotesController" in remotes_script.text
    audit_view_script = client.get("/ui/assets/audit_view.js")
    assert audit_view_script.status_code == 200
    assert audit_view_script.headers["x-content-type-options"] == "nosniff"
    assert "export function createAuditView" in audit_view_script.text
    audit_script = client.get("/ui/assets/audit.js")
    assert audit_script.status_code == 200
    assert audit_script.headers["x-content-type-options"] == "nosniff"
    assert "export function createAuditController" in audit_script.text
    sessions_script = client.get("/ui/assets/sessions.js")
    assert sessions_script.status_code == 200
    assert sessions_script.headers["x-content-type-options"] == "nosniff"
    assert "export function createSessionsController" in sessions_script.text
    terminal_script = client.get("/ui/assets/terminal.js")
    assert terminal_script.status_code == 200
    assert terminal_script.headers["x-content-type-options"] == "nosniff"
    assert "export function createTerminalController" in terminal_script.text
    files_script = client.get("/ui/assets/files.js")
    assert files_script.status_code == 200
    assert files_script.headers["x-content-type-options"] == "nosniff"
    assert "export function createFilesController" in files_script.text
    assert 'code_challenge_method", "S256"' in script.text
    assert "crypto.subtle.digest" in script.text
    assert 'resource: String(oauth.resource || "")' in script.text
    assert "pending.redirectUri === callbackUrl()" in script.text
    assert "OAuth issuer verification failed" in script.text
    assert "function setActiveView" in script.text
    assert "history.pushState" in script.text
    assert "response.status === 401" in script.text
    assert (
        "response.status === 401 || response.status === 403" not in script.text
    )
    assert "payload.message || payload.detail" in script.text
    assert "controllerState.generation" in dashboard_script.text
    assert (
        "requestedMachine !== controllerState.machine" in dashboard_script.text
    )
    assert "refreshDashboardInBackground" in dashboard_script.text
    assert "request(dashboardQueryPath())" in dashboard_script.text
    assert "createElementNS" in dashboard_script.text
    assert "dashboardNumber" in dashboard_script.text
    assert "controllerState.generation" in remotes_script.text
    assert "generation !== controllerState.generation" in remotes_script.text
    assert "startRemotePolling" in remotes_script.text
    assert "clearRemoteInviteResult" in remotes_script.text
    assert (
        "navigator.clipboard.writeText(controllerState.inviteCommand)"
        in remotes_script.text
    )
    assert 'inviteCommand: ""' in remotes_script.text
    assert "innerHTML" not in script.text
    assert "controllerState.terminalMachineStates" in terminal_script.text
    assert (
        "requestedMachine !== controllerState.terminalMachine"
        in terminal_script.text
    )
    assert 'url.searchParams.set("machine", machine)' in terminal_script.text
    assert 'url.searchParams.set("mode", "auto")' in terminal_script.text
    assert 'socket.binaryType = "arraybuffer"' in terminal_script.text
    assert "controllerState.terminalReady" in terminal_script.text
    assert "activateTerminalMode" in terminal_script.text
    assert "sendTerminalBytes" in terminal_script.text
    assert "offset += 65536" in terminal_script.text
    assert "registerOscHandler(8" in terminal_script.text
    assert "createImageAddon" in terminal_script.text
    assert terminal_script.text.index("loadAddon(api.createImageAddon())") < (
        terminal_script.text.index(
            "controllerState.terminalFitAddon = new api.FitAddon()"
        )
    )
    assert "allowNonHttpProtocols: false" in terminal_script.text
    assert (
        "controllerState.terminalSocketMachine === controllerState.terminalMachine"
        in terminal_script.text
    )
    assert "bridge_id" not in terminal_script.text
    assert "acceptTerminalSnapshot" in terminal_script.text
    assert "controllerState.terminalPendingOutput" in terminal_script.text
    assert "const terminalSpecialKeys = Object.freeze" in terminal_script.text
    assert "const terminalHistoryLimit = 100;" in terminal_script.text
    assert "navigateTerminalHistory" in terminal_script.text
    assert "WorkgateTerminalRenderer" in terminal_script.text
    assert "controllerState.filePreviewGeneration" in files_script.text
    assert (
        "generation !== controllerState.filePreviewGeneration"
        in files_script.text
    )
    assert "controllerState.fileListGeneration" in files_script.text
    assert (
        "requestedMachine !== controllerState.fileMachine" in files_script.text
    )
    assert 'fileQuery("/files/preview"' in files_script.text
    assert "machine: controllerState.fileMachine" in files_script.text
    assert "renderFileMachines" in files_script.text
    assert "controllerState.fileMutations" in files_script.text
    assert 'fileAction("copy"' in files_script.text
    assert 'fileAction("move"' in files_script.text
    assert 'fileAction("rename"' in files_script.text
    protected = client.get("/api/ui/bootstrap")
    assert protected.status_code == 401


def test_disabled_auth_ignores_stale_ui_session_cookie(monkeypatch, tmp_path):
    base_url = "https://workgate.example"
    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        base_url=base_url,
    )
    bearer = issue_access_token(
        client_id="stale-session-test",
        scope=default_scope(),
        resource=f"{base_url}/mcp",
    )
    session_token, _, _ = issue_ui_session(
        validate_bearer_token(bearer), UI_SESSION_BINDING
    )

    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="none",
        base_url=base_url,
    )
    client = TestClient(
        build_http_app(),
        base_url=base_url,
        client=("203.0.113.10", 50000),
    )
    response = client.get(
        "/api/ui/bootstrap",
        headers={
            "Cookie": f"{ui_session_cookie_name(base_url)}={session_token}",
            "Origin": base_url,
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["machines"][0]["name"] == "local"


def test_localhost_bypass_ignores_ui_session_cookies(monkeypatch, tmp_path):
    base_url = "https://workgate.example"
    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        mode="http",
        base_url=base_url,
        auth_bypass_localhost=True,
    )
    client = TestClient(
        build_http_app(),
        base_url=base_url,
        client=("127.0.0.1", 50000),
    )
    cookie_name = ui_session_cookie_name(base_url)

    invalid = client.get(
        "/api/ui/bootstrap",
        headers={"Cookie": f"{cookie_name}=not-a-jwt"},
    )
    assert invalid.status_code == 200

    bearer = issue_access_token(
        client_id="localhost-bypass-test",
        scope="shell:read",
        resource=f"{base_url}/mcp",
    )
    session_token, _, _ = issue_ui_session(
        validate_bearer_token(bearer), UI_SESSION_BINDING
    )
    reduced_scope = client.get(
        "/api/ui/remotes",
        headers={
            "Cookie": f"{cookie_name}={session_token}",
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert reduced_scope.status_code == 200


@pytest.mark.parametrize(
    ("browser_origin", "transport_origin"),
    (
        (
            "https://workgate.example",
            "https://workgate.example",
        ),
        ("http://localhost:8765", "http://localhost:8765"),
        (
            "https://workgate.example",
            "http://workgate.example",
        ),
    ),
    ids=("issuer-origin", "loopback-ui-origin", "tls-terminating-proxy"),
)
def test_browser_oauth_pkce_flow_reaches_authenticated_ui(
    monkeypatch, tmp_path, browser_origin, transport_origin
):
    base_url = "https://workgate.example"
    admin_pin = "12345678"
    oauth_state().clients.clear()
    oauth_state().codes.clear()
    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        base_url=base_url,
        oauth_admin_pin=admin_pin,
    )
    client = TestClient(
        build_http_app(),
        base_url=transport_origin,
        client=("203.0.113.10", 50000),
    )

    index = client.get("/ui")
    match = re.search(r'data-workgate-config="([^"]+)"', index.text)
    assert match is not None
    runtime = json.loads(html.unescape(match.group(1)))
    assert runtime["oauth"] == {
        "issuer": base_url,
        "resource": f"{base_url}/mcp",
        "scope": (
            "shell:read shell:write shell:execute git:write "
            "file:share remote:use audit:read audit:full"
        ),
        "registrationEndpoint": "/oauth/register",
        "authorizationEndpoint": "/oauth/authorize",
        "tokenEndpoint": "/oauth/token",
        "sessionOAuthEndpoint": "/api/ui/session/oauth",
        "sessionTokenEndpoint": "/api/ui/session/token",
        "sessionLogoutEndpoint": "/api/ui/session/logout",
    }
    csrf_cookie_name = ui_csrf_cookie_name(browser_origin)
    session_cookie_name = ui_session_cookie_name(browser_origin)
    assert runtime["csrfCookieName"] == csrf_cookie_name
    assert runtime["csrfHeaderName"] == UI_CSRF_HEADER
    assert runtime["sessionBindingHeaderName"] == UI_SESSION_BINDING_HEADER
    assert (
        runtime["sessionBindingProtocolPrefix"]
        == UI_SESSION_BINDING_PROTOCOL_PREFIX
    )
    assert runtime["sessionBindingStorageKey"] == UI_SESSION_BINDING_STORAGE_KEY
    assert (
        runtime["sessionEstablishedStorageKey"]
        == UI_SESSION_ESTABLISHED_STORAGE_KEY
    )

    invalid_session = client.get(
        "/api/ui/bootstrap",
        headers={
            "Cookie": f"{session_cookie_name}=not-a-jwt",
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert invalid_session.status_code == 401
    assert invalid_session.json()["detail"] == "Invalid Human UI session"

    callback = f"{browser_origin}/ui/callback"
    registration = client.post(
        "/oauth/register",
        json={
            "client_name": "Workgate WebUI",
            "redirect_uris": [callback],
        },
    )
    assert registration.status_code == 201
    client_id = registration.json()["client_id"]

    verifier = "browser-pkce-verifier-" + "x" * 48
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    state = "browser-oauth-state"
    authorization = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback,
        "scope": runtime["oauth"]["scope"],
        "resource": runtime["oauth"]["resource"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    approval_page = client.get("/oauth/authorize", params=authorization)
    assert approval_page.status_code == 200
    assert ">Approve<" in approval_page.text

    approved = client.post(
        "/oauth/authorize",
        data={**authorization, "pin": admin_pin},
        follow_redirects=False,
    )
    assert approved.status_code == 302
    redirect = urlparse(approved.headers["location"])
    query = parse_qs(redirect.query)
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == callback
    assert query["state"] == [state]
    assert query["iss"] == [runtime["oauth"]["issuer"]]
    assert client.get(approved.headers["location"]).status_code == 200

    exchange = client.post(
        "/api/ui/session/oauth",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "client_id": client_id,
            "redirect_uri": callback,
            "resource": runtime["oauth"]["resource"],
            "code_verifier": verifier,
        },
        headers={
            "Origin": browser_origin,
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert exchange.status_code == 200
    assert exchange.json()["ok"] is True
    assert "access_token" not in exchange.text
    set_cookies = exchange.headers.get_list("set-cookie")
    session_cookie = next(
        value
        for value in set_cookies
        if value.startswith(f"{session_cookie_name}=")
    )
    csrf_cookie = next(
        value
        for value in set_cookies
        if value.startswith(f"{csrf_cookie_name}=")
    )
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    for value in (session_cookie, csrf_cookie):
        max_age_match = re.search(r"Max-Age=(\d+)", value)
        assert max_age_match is not None
        assert 3590 <= int(max_age_match.group(1)) <= 3600
        assert "Path=/" in value
        assert "SameSite=strict" in value
        if browser_origin.startswith("https://"):
            assert "Secure" in value
        else:
            assert "Secure" not in value

    session_value = client.cookies.get(session_cookie_name)
    csrf_token = client.cookies.get(csrf_cookie_name)
    assert session_value
    assert csrf_token
    cookie_header = (
        f"{session_cookie_name}={session_value}; "
        f"{csrf_cookie_name}={csrf_token}"
    )

    bootstrap = client.get(
        "/api/ui/bootstrap",
        headers={
            "Cookie": cookie_header,
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert bootstrap.status_code == 200
    assert bootstrap.json()["data"]["machines"][0]["name"] == "local"

    csrf_rejected = client.post(
        "/api/ui/terminals/start",
        json={},
        headers={
            "Cookie": cookie_header,
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert csrf_rejected.status_code == 403
    assert csrf_rejected.json()["detail"] == "Human UI CSRF validation failed"

    unrelated = client.get("/tools/list_persistent_shells")
    assert unrelated.status_code == 401

    logout = client.post(
        "/api/ui/session/logout",
        headers={
            "Cookie": cookie_header,
            "Origin": browser_origin,
            UI_CSRF_HEADER: csrf_token,
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert logout.status_code == 200
    assert client.get("/api/ui/bootstrap").status_code == 401


def test_ui_session_token_is_cryptographically_isolated_from_oauth_bearer(
    monkeypatch, tmp_path
):
    base_url = "https://workgate.example"
    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        base_url=base_url,
    )
    bearer = issue_access_token(
        client_id="browser-test",
        scope=default_scope(),
        resource=f"{base_url}/mcp",
    )
    bearer_claims = validate_bearer_token(bearer)
    session_token, csrf_token, max_age = issue_ui_session(
        bearer_claims, UI_SESSION_BINDING
    )

    assert csrf_token
    assert max_age is not None and 3590 <= max_age <= 3600
    assert validate_ui_session(session_token, UI_SESSION_BINDING)[
        "client_id"
    ] == ("browser-test")
    with pytest.raises(jwt.InvalidTokenError):
        validate_ui_session(session_token, "w" * 43)
    with pytest.raises(jwt.PyJWTError):
        validate_bearer_token(session_token)
    with pytest.raises(jwt.PyJWTError):
        validate_ui_session(bearer, UI_SESSION_BINDING)

    browser_origin = "http://localhost:8765"
    origin_session, _, _ = issue_ui_session(
        bearer_claims,
        UI_SESSION_BINDING,
        browser_origin,
    )
    assert (
        validate_ui_session(
            origin_session,
            UI_SESSION_BINDING,
            browser_origin,
        )["client_id"]
        == "browser-test"
    )
    with pytest.raises(jwt.InvalidAudienceError):
        validate_ui_session(origin_session, UI_SESSION_BINDING, base_url)

    short_expiry = int(time.time()) + 90
    short_token, _, short_max_age = issue_ui_session(
        {**bearer_claims, "exp": short_expiry}, UI_SESSION_BINDING
    )
    assert short_max_age is not None and 1 <= short_max_age <= 90
    assert (
        validate_ui_session(short_token, UI_SESSION_BINDING)["exp"]
        == short_expiry
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        issue_ui_session(
            {**bearer_claims, "exp": int(time.time()) - 1},
            UI_SESSION_BINDING,
        )


def test_ui_session_remains_persistent_for_unbounded_bearer(
    monkeypatch, tmp_path
):
    base_url = "https://workgate.example"
    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        base_url=base_url,
        oauth_access_token_ttl_s=0,
    )
    bearer = issue_access_token(
        client_id="unbounded-browser-test",
        scope=default_scope(),
        resource=f"{base_url}/mcp",
    )
    bearer_claims = validate_bearer_token(bearer)
    assert "exp" not in bearer_claims

    session_token, _, max_age = issue_ui_session(
        bearer_claims, UI_SESSION_BINDING
    )
    assert max_age == UI_SESSION_UNBOUNDED_SOURCE_TTL_S
    session_claims = validate_ui_session(session_token, UI_SESSION_BINDING)
    assert session_claims["exp"] - session_claims["iat"] == (
        UI_SESSION_UNBOUNDED_SOURCE_TTL_S
    )

    client = TestClient(
        build_http_app(),
        base_url=base_url,
        client=("203.0.113.10", 50000),
    )
    response = client.post(
        "/api/ui/session/token",
        headers={
            "Origin": base_url,
            "Authorization": f"Bearer {bearer}",
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all(
        f"Max-Age={UI_SESSION_UNBOUNDED_SOURCE_TTL_S}" in cookie
        for cookie in cookies
    )


def test_ui_cookie_names_are_isolated_by_full_origin():
    first_origin = "https://workgate.example:8443"
    second_origin = "https://workgate.example:9443"

    assert ui_session_cookie_name(first_origin) != ui_session_cookie_name(
        second_origin
    )
    assert ui_csrf_cookie_name(first_origin) != ui_csrf_cookie_name(
        second_origin
    )
    assert ui_session_cookie_name(
        f"{first_origin}/ui"
    ) == ui_session_cookie_name(first_origin)
    assert ui_csrf_cookie_name(f"{first_origin}/ui") == ui_csrf_cookie_name(
        first_origin
    )


def test_ui_origins_use_browser_canonicalization(monkeypatch, tmp_path):
    configured = "HTTPS://Workgate.Example:443/ui"
    canonical = "https://workgate.example"
    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        base_url=configured,
    )

    assert canonical_ui_origin(configured) == canonical
    assert canonical_ui_origin("http://Example.COM:80") == "http://example.com"
    assert canonical_ui_origin("https://faß.de") == "https://xn--fa-hia.de"
    assert canonical_ui_origin("https://xn--fa-hia.de") == (
        "https://xn--fa-hia.de"
    )
    assert canonical_ui_origin("https://例え.テスト") == (
        "https://xn--r8jz45g.xn--zckzah"
    )
    assert canonical_ui_origin("http://127.1:8765") == ("http://127.0.0.1:8765")
    assert canonical_ui_origin("http://0x7f.1") == "http://127.0.0.1"
    assert canonical_ui_origin("http://0177.1") == "http://127.0.0.1"
    assert canonical_ui_origin("http://2130706433") == "http://127.0.0.1"
    with pytest.raises(ValueError):
        canonical_ui_origin("http://256.1.1.1")
    with pytest.raises(ValueError):
        canonical_ui_origin("http://1.2.3.4.5")
    assert canonical_ui_origin("https://[2001:0DB8::1]:443") == (
        "https://[2001:db8::1]"
    )
    assert is_valid_ui_origin(canonical)
    assert is_valid_ui_origin("https://WORKGATE.EXAMPLE:443")
    assert not is_valid_ui_origin("https://workgate.example:444")
    assert not is_valid_ui_origin("null")
    assert ui_session_cookie_name(configured) == ui_session_cookie_name(
        canonical
    )
    assert ui_csrf_cookie_name(configured) == ui_csrf_cookie_name(canonical)


def test_ui_session_cookie_cannot_be_replayed_without_origin_binding(
    monkeypatch, tmp_path
):
    base_url = "https://workgate.example:8443"
    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        base_url=base_url,
    )
    bearer = issue_access_token(
        client_id="sibling-port-test",
        scope=default_scope(),
        resource=f"{base_url}/mcp",
    )
    session_token, _, _ = issue_ui_session(
        validate_bearer_token(bearer), UI_SESSION_BINDING
    )
    cookie = f"{ui_session_cookie_name(base_url)}={session_token}"
    client = TestClient(
        build_http_app(),
        base_url=base_url,
        client=("203.0.113.10", 50000),
    )

    stolen_cookie_headers = {"Cookie": cookie, "Origin": base_url}
    assert (
        client.get(
            "/api/ui/bootstrap", headers=stolen_cookie_headers
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/ui/bootstrap",
            headers={
                **stolen_cookie_headers,
                UI_SESSION_BINDING_HEADER: "w" * 43,
            },
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/ui/bootstrap",
            headers={
                **stolen_cookie_headers,
                UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
            },
        ).status_code
        == 200
    )


def test_existing_bearer_can_be_converted_without_exposing_it_to_storage(
    monkeypatch, tmp_path
):
    base_url = "https://workgate.example"
    _configure_ui(
        monkeypatch,
        tmp_path,
        auth_mode="oauth",
        base_url=base_url,
    )
    client = TestClient(
        build_http_app(),
        base_url=base_url,
        client=("203.0.113.10", 50000),
    )
    token = issue_access_token(
        client_id="manual-browser-test",
        scope=default_scope(),
        resource=f"{base_url}/mcp",
    )

    rejected = client.post(
        "/api/ui/session/token",
        headers={
            "Origin": "https://attacker.example",
            "Authorization": f"Bearer {token}",
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert rejected.status_code == 403

    missing_binding = client.post(
        "/api/ui/session/token",
        headers={"Origin": base_url, "Authorization": f"Bearer {token}"},
    )
    assert missing_binding.status_code == 400
    assert (
        missing_binding.json()["detail"] == "Invalid Human UI session binding"
    )

    converted = client.post(
        "/api/ui/session/token",
        headers={
            "Origin": base_url,
            "Authorization": f"Bearer {token}",
            UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING,
        },
    )
    assert converted.status_code == 200
    assert token not in converted.text
    assert "access_token" not in converted.text
    assert (
        client.get(
            "/api/ui/bootstrap",
            headers={UI_SESSION_BINDING_HEADER: UI_SESSION_BINDING},
        ).status_code
        == 200
    )
    assert client.get("/api/ui/bootstrap").status_code == 401
    assert (
        client.get(
            "/api/ui/bootstrap",
            headers={UI_SESSION_BINDING_HEADER: "w" * 43},
        ).status_code
        == 401
    )


def test_local_ui_token_bypasses_oauth_only_on_loopback(monkeypatch, tmp_path):
    _configure_ui(monkeypatch, tmp_path, auth_mode="oauth")
    token = get_or_create_ui_local_token()
    headers = {UI_LOCAL_TOKEN_HEADER: token}

    loopback = TestClient(build_http_app(), client=("127.0.0.1", 50000))
    response = loopback.get("/api/ui/bootstrap", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["machines"][0]["name"] == "local"

    unrelated = loopback.get("/tools/list_persistent_shells", headers=headers)
    assert unrelated.status_code == 401

    external = TestClient(build_http_app(), client=("203.0.113.10", 50000))
    rejected = external.get("/api/ui/bootstrap", headers=headers)
    assert rejected.status_code == 401

    token_path = tmp_path / ".state" / "ui" / "local-token"
    assert token_path.read_text(encoding="utf-8").strip() == token
    if os.name != "nt":
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_human_ui_custom_mount_and_bootstrap(monkeypatch, tmp_path):
    _configure_ui(monkeypatch, tmp_path, ui_path="/control")
    client = TestClient(build_http_app())

    assert client.get("/ui").status_code == 404
    index = client.get("/control")
    assert index.status_code == 200
    assert re.search(
        r'href="/control/assets/web\.css\?v=[0-9a-f]{16}"', index.text
    )
    assert re.search(
        r'src="/control/assets/syntax_highlight\.js\?v=[0-9a-f]{16}"',
        index.text,
    )
    match = re.search(r'data-workgate-config="([^"]+)"', index.text)
    assert match is not None
    runtime = json.loads(html.unescape(match.group(1)))
    assert runtime["oauth"] is None
    assert runtime["wallpaper"] == "aurora"

    payload = client.get("/api/ui/bootstrap").json()["data"]
    assert payload["ui"] == {
        "path": "/control",
        "api_prefix": "/api/ui",
        "auth_mode": "none",
        "features": {
            "dashboard": True,
            "remote_dashboard": True,
            "machines": True,
            "remotes": True,
            "terminals": True,
            "remote_terminals": True,
            "terminal_websocket": True,
            "files": True,
            "file_preview": True,
            "syntax_highlighting": True,
            "audit_image_preview": True,
            "wallpaper": "aurora",
            "opentui": True,
            "file_editor": True,
            "file_copy": True,
            "file_move": True,
            "file_rename": True,
            "remote_files": True,
            "remote_file_editor": True,
            "sessions": True,
            "remote_sessions": True,
            "todos": True,
            "remote_todos": True,
            "audit": True,
            "remote_audit": True,
        },
    }
    assert payload["counts"] == {"online": 1, "offline": 0, "total": 1}
    assert payload["machines"][0]["workdir"] == str(tmp_path)


def test_human_ui_can_be_disabled(monkeypatch, tmp_path):
    _configure_ui(monkeypatch, tmp_path, ui_enabled=False)
    client = TestClient(build_http_app())

    assert client.get("/ui").status_code == 404
    assert client.get("/api/ui/bootstrap").status_code == 404


def test_machine_inventory_includes_remote_rows(monkeypatch, tmp_path):
    _configure_ui(monkeypatch, tmp_path, remote_enabled=True)

    class FakeInventory:
        def model_dump(self, *, mode):
            assert mode == "json"
            return {
                "machines": [
                    {
                        "name": "worker-a",
                        "status": "offline",
                        "workdir": "/srv/work",
                        "last_seen": 0,
                        "last_seen_age_s": None,
                        "offline_after_s": 60,
                        "queue_depth": 0,
                        "capabilities": ["shell"],
                        "info": {"platform": "linux"},
                    }
                ],
                "counts": {"online": 0, "offline": 1, "total": 1},
            }

    class FakeManager:
        def list_machines(self):
            return FakeInventory()

    monkeypatch.setattr(human_ui_module, "remote_manager", FakeManager)
    payload = (
        TestClient(build_http_app()).get("/api/ui/machines").json()["data"]
    )

    assert [item["name"] for item in payload["machines"]] == [
        "local",
        "worker-a",
    ]
    assert payload["counts"] == {"online": 1, "offline": 1, "total": 2}
