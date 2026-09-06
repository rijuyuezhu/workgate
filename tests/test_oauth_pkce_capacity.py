import base64
import hashlib
import itertools
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import _add_public_routes_to_mcp_http_app
from workgate.oauth.core import service as oauth_service
from workgate.oauth.core.client_store import client_store_path
from workgate.oauth.core.models import AuthCode, OAuthClient
from workgate.oauth.core.requests import AuthorizationRequestInput
from workgate.oauth.core.state import (
    build_oauth_state,
    configure_oauth_state,
    oauth_state,
)

BASE_URL = "https://workgate.example.com"
RESOURCE_URL = f"{BASE_URL}/mcp"
REDIRECT_URL = "https://client.example/callback"
ADMIN_PIN = "1234"


@pytest.fixture(autouse=True)
def _reset_oauth_state(tmp_path):
    clear_settings_cache()
    state = build_oauth_state(tmp_path / ".state")
    previous = configure_oauth_state(state)
    try:
        yield
    finally:
        configure_oauth_state(previous)
        clear_settings_cache()


@pytest.fixture
def oauth_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", ADMIN_PIN)
    clear_settings_cache()
    return TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])


def _s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _register(client: TestClient, *, suffix: str = "") -> str:
    response = client.post(
        "/oauth/register",
        json={
            "client_name": f"PKCE client{suffix}",
            "redirect_uris": [f"{REDIRECT_URL}{suffix}"],
        },
    )
    assert response.status_code == 201
    return response.json()["client_id"]


def _authorization_data(
    client_id: str,
    verifier: str,
    *,
    suffix: str = "",
    method: str | None = "S256",
    challenge: str | None = None,
) -> dict[str, str]:
    data = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": f"{REDIRECT_URL}{suffix}",
        "resource": RESOURCE_URL,
        "code_challenge": challenge or _s256_challenge(verifier),
        "pin": ADMIN_PIN,
    }
    if method is not None:
        data["code_challenge_method"] = method
    return data


def _authorize(
    client: TestClient,
    client_id: str,
    verifier: str,
    *,
    suffix: str = "",
    method: str | None = "S256",
    challenge: str | None = None,
):
    return client.post(
        "/oauth/authorize",
        data=_authorization_data(
            client_id,
            verifier,
            suffix=suffix,
            method=method,
            challenge=challenge,
        ),
        follow_redirects=False,
    )


def _authorization_code(response) -> str:
    assert response.status_code == 302
    return parse_qs(urlparse(response.headers["location"]).query)["code"][0]


def _exchange(
    client: TestClient,
    *,
    code: str,
    client_id: str,
    verifier: str,
    suffix: str = "",
):
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": f"{REDIRECT_URL}{suffix}",
            "resource": RESOURCE_URL,
            "code_verifier": verifier,
        },
    )


def test_oauth_metadata_advertises_only_s256(oauth_client):
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ):
        response = oauth_client.get(path)
        assert response.status_code == 200
        assert response.json()["code_challenge_methods_supported"] == ["S256"]


@pytest.mark.parametrize(
    ("method", "expected_error"),
    [
        (None, "Missing code_challenge_method"),
        ("plain", "Only code_challenge_method=S256 is supported"),
        ("S512", "Only code_challenge_method=S256 is supported"),
    ],
)
def test_authorization_requires_explicit_s256(
    oauth_client, method, expected_error
):
    client_id = _register(oauth_client)
    response = _authorize(oauth_client, client_id, "v" * 64, method=method)

    assert response.status_code == 200
    assert expected_error in response.text
    assert oauth_state().codes == {}
    assert oauth_state().clients[client_id].approved_at is None


@pytest.mark.parametrize("challenge", ["x" * 42, "x" * 129, "x" * 42 + "!"])
def test_authorization_rejects_malformed_pkce_challenge(
    oauth_client, challenge
):
    client_id = _register(oauth_client)
    response = _authorize(
        oauth_client,
        client_id,
        "v" * 64,
        challenge=challenge,
    )

    assert response.status_code == 200
    assert "Invalid code_challenge" in response.text
    assert oauth_state().codes == {}


@pytest.mark.parametrize("verifier", ["v" * 42, "v" * 129, "v" * 42 + "!"])
def test_token_exchange_rejects_malformed_pkce_verifier_without_consuming_code(
    oauth_client, verifier
):
    valid_verifier = "v" * 64
    client_id = _register(oauth_client)
    code = _authorization_code(
        _authorize(oauth_client, client_id, valid_verifier)
    )

    rejected = _exchange(
        oauth_client,
        code=code,
        client_id=client_id,
        verifier=verifier,
    )
    assert rejected.status_code == 400
    assert rejected.json() == {
        "error": "invalid_grant",
        "error_description": "PKCE verification failed",
    }
    assert code in oauth_state().codes

    accepted = _exchange(
        oauth_client,
        code=code,
        client_id=client_id,
        verifier=valid_verifier,
    )
    assert accepted.status_code == 200
    assert code not in oauth_state().codes


def test_valid_s256_flow_rejects_wrong_verifier_and_code_reuse(oauth_client):
    verifier = "v" * 64
    client_id = _register(oauth_client)
    code = _authorization_code(_authorize(oauth_client, client_id, verifier))

    wrong = _exchange(
        oauth_client,
        code=code,
        client_id=client_id,
        verifier="w" * 64,
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"
    assert code in oauth_state().codes

    accepted = _exchange(
        oauth_client,
        code=code,
        client_id=client_id,
        verifier=verifier,
    )
    assert accepted.status_code == 200
    assert code not in oauth_state().codes

    reused = _exchange(
        oauth_client,
        code=code,
        client_id=client_id,
        verifier=verifier,
    )
    assert reused.status_code == 400
    assert reused.json() == {
        "error": "invalid_grant",
        "error_description": "Unknown or used code",
    }


def test_pending_code_capacity_preserves_live_code_and_pending_client(
    oauth_client, monkeypatch
):
    monkeypatch.setenv("WORKGATE_OAUTH_MAX_PENDING_CODES", "1")
    clear_settings_cache()

    first_verifier = "a" * 64
    first_client_id = _register(oauth_client, suffix="-first")
    first_code = _authorization_code(
        _authorize(
            oauth_client,
            first_client_id,
            first_verifier,
            suffix="-first",
        )
    )

    second_client_id = _register(oauth_client, suffix="-second")
    blocked = _authorize(
        oauth_client,
        second_client_id,
        "b" * 64,
        suffix="-second",
    )

    assert blocked.status_code == 200
    assert (
        "Too many pending authorization requests; try again later"
        in blocked.text
    )
    assert set(oauth_state().codes) == {first_code}
    assert oauth_state().clients[second_client_id].approved_at is None

    persisted = json.loads(client_store_path().read_text(encoding="utf-8"))
    assert [item["client_id"] for item in persisted["clients"]] == [
        first_client_id
    ]

    first_exchange = _exchange(
        oauth_client,
        code=first_code,
        client_id=first_client_id,
        verifier=first_verifier,
        suffix="-first",
    )
    assert first_exchange.status_code == 200

    second = _authorize(
        oauth_client,
        second_client_id,
        "b" * 64,
        suffix="-second",
    )
    assert second.status_code == 302
    assert oauth_state().clients[second_client_id].approved_at is not None


@pytest.mark.parametrize("used", [False, True])
def test_expired_or_used_code_pruning_restores_capacity(
    oauth_client, monkeypatch, used
):
    monkeypatch.setenv("WORKGATE_OAUTH_MAX_PENDING_CODES", "1")
    monkeypatch.setenv("WORKGATE_OAUTH_CODE_TTL_S", "10")
    clear_settings_cache()

    stale_code = "stale"
    oauth_state().codes[stale_code] = AuthCode(
        code=stale_code,
        client_id="old-client",
        redirect_uri=REDIRECT_URL,
        scope="shell:read",
        resource=RESOURCE_URL,
        code_challenge=_s256_challenge("s" * 64),
        code_challenge_method="S256",
        created_at=int(time.time()) if used else 0,
        used=used,
    )

    client_id = _register(oauth_client)
    response = _authorize(oauth_client, client_id, "n" * 64)

    assert response.status_code == 302
    assert stale_code not in oauth_state().codes
    assert len(oauth_state().codes) == 1


def test_zero_pending_code_capacity_limit_allows_multiple_live_codes(
    oauth_client, monkeypatch
):
    monkeypatch.setenv("WORKGATE_OAUTH_MAX_PENDING_CODES", "0")
    clear_settings_cache()

    client_id = _register(oauth_client)
    first = _authorize(oauth_client, client_id, "a" * 64)
    second = _authorize(oauth_client, client_id, "b" * 64)

    assert first.status_code == 302
    assert second.status_code == 302
    assert len(oauth_state().codes) == 2


def test_capacity_rejection_survives_audit_failure(oauth_client, monkeypatch):
    monkeypatch.setenv("WORKGATE_OAUTH_MAX_PENDING_CODES", "1")
    clear_settings_cache()
    oauth_state().codes["live"] = AuthCode(
        code="live",
        client_id="existing-client",
        redirect_uri=REDIRECT_URL,
        scope="shell:read",
        resource=RESOURCE_URL,
        code_challenge=_s256_challenge("e" * 64),
        code_challenge_method="S256",
    )
    client_id = _register(oauth_client)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(oauth_service, "audit", fail_audit)
    response = _authorize(oauth_client, client_id, "n" * 64)

    assert response.status_code == 200
    assert (
        "Too many pending authorization requests; try again later"
        in response.text
    )
    assert set(oauth_state().codes) == {"live"}
    assert oauth_state().clients[client_id].approved_at is None


def test_concurrent_authorization_respects_pending_code_capacity(
    oauth_client, monkeypatch
):
    monkeypatch.setenv("WORKGATE_OAUTH_MAX_PENDING_CODES", "1")
    clear_settings_cache()
    client_id = "approved-client"
    oauth_state().clients[client_id] = OAuthClient(
        client_id=client_id,
        redirect_uris=[REDIRECT_URL],
        approved_at=int(time.time()),
    )
    verifier = "c" * 64
    request = oauth_service.validate_authorization_request(
        AuthorizationRequestInput(
            response_type="code",
            client_id=client_id,
            redirect_uri=REDIRECT_URL,
            resource=RESOURCE_URL,
            code_challenge=_s256_challenge(verifier),
            code_challenge_method="S256",
        )
    )
    code_counter = itertools.count()

    def slow_code_generation(_state=None) -> str:
        time.sleep(0.05)
        return f"concurrent-code-{next(code_counter)}"

    monkeypatch.setattr(
        oauth_service, "_new_authorization_code", slow_code_generation
    )
    monkeypatch.setattr(oauth_service, "audit", lambda *args, **kwargs: None)

    def issue() -> str:
        try:
            oauth_service.issue_authorization_response(request)
        except oauth_service.OAuthStateCapacityError:
            return "capacity"
        return "issued"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: issue(), range(2)))

    assert sorted(results) == ["capacity", "issued"]
    assert len(oauth_state().codes) == 1
