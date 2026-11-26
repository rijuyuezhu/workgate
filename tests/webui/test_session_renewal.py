"""Native session grant regression contracts adapted from pinned upstream auth edges."""

import time

import jwt
import pytest

from workgate.config.settings import clear_settings_cache
from workgate.oauth.core.models import OAuthClient
from workgate.oauth.core.state import OAuthState, use_oauth_state
from workgate.oauth.protocol.token_codec import (
    issue_access_token,
    validate_bearer_token,
)
from workgate.persistence import FileStateStore
from workgate.ui import session_registry
from workgate.ui.session import (
    issue_ui_session,
    renew_ui_session,
    revoke_ui_session,
    validate_ui_session,
)

ORIGIN = "https://workgate.example"
CALLBACK = ORIGIN + "/ui/callback"
BINDING = "b" * 43
CLIENT = "native-browser"


@pytest.fixture
def oauth(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKGATE_BASE_URL", ORIGIN)
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_UI_PATH", "/ui")
    clear_settings_cache()
    state = OAuthState(tmp_path / "control")
    state.clients[CLIENT] = OAuthClient(
        CLIENT,
        redirect_uris=[CALLBACK],
        client_name="Workgate WebUI",
        approved_at=int(time.time()),
    )
    with use_oauth_state(state):
        yield state
    clear_settings_cache()


def bearer_claims():
    return validate_bearer_token(
        issue_access_token(
            client_id=CLIENT,
            scope="shell:read",
            resource=ORIGIN + "/mcp",
        )
    )


def test_renewable_grant_slides_and_survives_repository_restart(oauth):
    token, csrf, age = issue_ui_session(
        bearer_claims(), BINDING, ORIGIN, first_party_redirect=CALLBACK
    )
    assert csrf and age == session_registry.SESSION_TTL
    claims = validate_ui_session(token, BINDING, ORIGIN)
    store = session_registry.WebAuthStore(oauth.state_store)
    identity = claims["jti"]
    assert store.session(identity) == claims["exp"]
    assert identity not in store.path.read_text(encoding="utf-8")
    restarted = session_registry.WebAuthStore(
        FileStateStore(lambda: oauth.state_dir)
    )
    assert restarted.session(identity) == claims["exp"]
    # Shorten the stored deadline to simulate approaching the expiry without
    # changing system time or signed-cookie validation.
    with oauth.state_store.transaction(store.path):
        oauth.state_store.write_json(
            store.path, {store._hash(identity): int(time.time()) + 5}
        )
    renewed = renew_ui_session(claims, ORIGIN)
    assert renewed is not None
    new_token, new_age = renewed
    assert new_age > session_registry.SESSION_TTL - 10
    new_claims = validate_ui_session(new_token, BINDING, ORIGIN)
    assert new_claims["jti"] == identity
    assert new_claims["scope"] == claims["scope"] == "shell:read"
    assert store.session(identity) == new_claims["exp"]
    revoke_ui_session(new_claims)
    for old in (token, new_token):
        with pytest.raises(jwt.InvalidTokenError):
            validate_ui_session(old, BINDING, ORIGIN)
    assert store.renew(identity) is None


@pytest.mark.parametrize(
    "change",
    ["unapproved", "name", "redirects", "different-callback", "bearer"],
)
def test_only_exact_approved_first_party_callback_is_promoted(oauth, change):
    client = oauth.clients[CLIENT]
    redirect = CALLBACK
    if change == "unapproved":
        client.approved_at = None
    elif change == "name":
        client.client_name = "Other app"
    elif change == "redirects":
        client.redirect_uris.append(ORIGIN + "/other")
    elif change == "different-callback":
        redirect = ORIGIN + "/other"
    else:
        redirect = None
    source = {**bearer_claims(), "exp": int(time.time()) + 60}
    token, _, age = issue_ui_session(
        source, BINDING, ORIGIN, first_party_redirect=redirect
    )
    claims = validate_ui_session(token, BINDING, ORIGIN)
    assert age is not None and 0 < age <= 60
    assert claims["exp"] == source["exp"]
    assert "renewable_approved_at" not in claims
    assert renew_ui_session(claims, ORIGIN) is None
    assert not oauth.state_store.layout.ui_sessions_path.exists()


@pytest.mark.parametrize("change", ["removed", "approval", "callback"])
def test_live_client_approval_is_rechecked(oauth, change):
    token, _, _ = issue_ui_session(
        bearer_claims(), BINDING, ORIGIN, first_party_redirect=CALLBACK
    )
    claims = validate_ui_session(token, BINDING, ORIGIN)
    if change == "removed":
        del oauth.clients[CLIENT]
    elif change == "approval":
        oauth.clients[CLIENT].approved_at = (
            int(claims["renewable_approved_at"]) + 1
        )
    else:
        oauth.clients[CLIENT].redirect_uris = [ORIGIN + "/other"]
    with pytest.raises(jwt.InvalidTokenError):
        validate_ui_session(token, BINDING, ORIGIN)
    with pytest.raises(jwt.InvalidTokenError):
        renew_ui_session(claims, ORIGIN)


def test_registry_capacity_expiry_and_corruption(oauth, monkeypatch):
    store = session_registry.WebAuthStore(oauth.state_store)
    monkeypatch.setattr(session_registry, "_MAX_SESSIONS", 1)
    now = int(time.time())
    store.issue("one", now + 10)
    with pytest.raises(ValueError, match="capacity"):
        store.issue("two", now + 10)
    with pytest.raises(ValueError, match="deadline"):
        store.issue("expired", now - 1)
    with pytest.raises(ValueError, match="deadline"):
        store.issue("future", now + session_registry.SESSION_TTL + 10)
    with oauth.state_store.transaction(store.path):
        oauth.state_store.write_json(store.path, {store._hash("one"): now - 1})
    assert store.session("one") is None and store.renew("one") is None
    store.issue("two", now + 10)
    store.revoke("one")
    assert store.session("two") == now + 10
    with oauth.state_store.transaction(store.path):
        oauth.state_store.write_json(store.path, {"invalid": "record"})
    with pytest.raises(ValueError, match="Invalid browser session registry"):
        store.session("two")
