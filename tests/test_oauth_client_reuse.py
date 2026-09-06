import itertools
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import _add_public_routes_to_mcp_http_app
from workgate.oauth.core import service as oauth_service
from workgate.oauth.core.models import OAuthClient
from workgate.oauth.core.requests import RegistrationRequest
from workgate.oauth.core.state import (
    build_oauth_state,
    configure_oauth_state,
    oauth_state,
)

BASE_URL = "https://workgate.example.com"
REDIRECT_A = "https://client.example/callback-a"
REDIRECT_B = "https://client.example/callback-b"
CLIENT_NAME = "Reusable public client"


@pytest.fixture(autouse=True)
def _reset_oauth_state(tmp_path):
    clear_settings_cache()
    state = build_oauth_state(tmp_path / "oauth-owner")
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
    clear_settings_cache()
    return TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])


def _registration_body(
    *,
    client_name: str = CLIENT_NAME,
    redirect_uris: list[str] | None = None,
) -> dict[str, object]:
    return {
        "client_name": client_name,
        "redirect_uris": redirect_uris or [REDIRECT_A, REDIRECT_B],
    }


def test_registration_reuses_matching_client_ignoring_uri_order_and_duplicates(
    oauth_client,
):
    first = oauth_client.post(
        "/oauth/register",
        json=_registration_body(redirect_uris=[REDIRECT_B, REDIRECT_A]),
    )
    repeated = oauth_client.post(
        "/oauth/register",
        json=_registration_body(
            redirect_uris=[REDIRECT_A, REDIRECT_B, REDIRECT_A]
        ),
    )

    assert first.status_code == 201
    assert first.json()["reused"] is False
    assert repeated.status_code == 200
    assert repeated.json()["reused"] is True
    assert repeated.json()["client_id"] == first.json()["client_id"]
    assert repeated.json()["redirect_uris"] == [REDIRECT_B, REDIRECT_A]
    assert len(oauth_state().clients) == 1


def test_registration_reuse_does_not_consume_pending_capacity(
    oauth_client, monkeypatch
):
    monkeypatch.setenv("WORKGATE_OAUTH_MAX_DYNAMIC_CLIENTS", "1")
    clear_settings_cache()

    first = oauth_client.post("/oauth/register", json=_registration_body())
    reused = oauth_client.post(
        "/oauth/register",
        json=_registration_body(redirect_uris=[REDIRECT_B, REDIRECT_A]),
    )
    blocked = oauth_client.post(
        "/oauth/register",
        json=_registration_body(redirect_uris=[REDIRECT_A]),
    )

    assert first.status_code == 201
    assert reused.status_code == 200
    assert reused.json()["client_id"] == first.json()["client_id"]
    assert blocked.status_code == 400
    assert blocked.json()["error_description"] == (
        "Too many pending OAuth client registrations"
    )
    assert len(oauth_state().clients) == 1


def test_registration_keeps_distinct_names_and_uri_sets_separate(oauth_client):
    first = oauth_client.post("/oauth/register", json=_registration_body())
    different_name = oauth_client.post(
        "/oauth/register",
        json=_registration_body(client_name="Another public client"),
    )
    different_uris = oauth_client.post(
        "/oauth/register",
        json=_registration_body(redirect_uris=[REDIRECT_A]),
    )

    assert [
        first.status_code,
        different_name.status_code,
        different_uris.status_code,
    ] == [
        201,
        201,
        201,
    ]
    assert (
        len(
            {
                first.json()["client_id"],
                different_name.json()["client_id"],
                different_uris.json()["client_id"],
            }
        )
        == 3
    )
    assert len(oauth_state().clients) == 3


def test_registration_prefers_approved_then_oldest_matching_client(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_OAUTH_CLIENT_TTL_S", "0")
    clear_settings_cache()
    request = RegistrationRequest(
        redirect_uris=(REDIRECT_B, REDIRECT_A),
        client_name=CLIENT_NAME,
    )
    oauth_state().clients["pending-old"] = OAuthClient(
        client_id="pending-old",
        redirect_uris=[REDIRECT_A, REDIRECT_B],
        client_name=CLIENT_NAME,
        created_at=10,
    )
    oauth_state().clients["approved-new"] = OAuthClient(
        client_id="approved-new",
        redirect_uris=[REDIRECT_A, REDIRECT_B],
        client_name=CLIENT_NAME,
        created_at=20,
        approved_at=21,
    )

    approved = oauth_service.register_dynamic_client(request)
    assert approved.reused is True
    assert approved.client.client_id == "approved-new"

    oauth_state().clients.pop("approved-new")
    oauth_state().clients["pending-new"] = OAuthClient(
        client_id="pending-new",
        redirect_uris=[REDIRECT_A, REDIRECT_B],
        client_name=CLIENT_NAME,
        created_at=20,
    )
    pending = oauth_service.register_dynamic_client(request)
    assert pending.reused is True
    assert pending.client.client_id == "pending-old"


def test_registration_reuses_approved_client_after_memory_reload(
    oauth_client, monkeypatch, tmp_path
):
    first = oauth_client.post("/oauth/register", json=_registration_body())
    client_id = first.json()["client_id"]
    oauth_service._approve_client(client_id, now=int(time.time()) + 1)

    reloaded_state = build_oauth_state(tmp_path / ".state")
    assert reloaded_state.start() == 1
    previous = configure_oauth_state(reloaded_state)
    try:
        reloaded_client = TestClient(
            _add_public_routes_to_mcp_http_app(Starlette())[0]
        )
        repeated = reloaded_client.post(
            "/oauth/register",
            json=_registration_body(redirect_uris=[REDIRECT_B, REDIRECT_A]),
        )

        assert repeated.status_code == 200
        assert repeated.json()["reused"] is True
        assert repeated.json()["client_id"] == client_id
        assert oauth_state().clients[client_id].approved_at is not None
        assert len(oauth_state().clients) == 1
    finally:
        configure_oauth_state(previous)


def test_concurrent_matching_registrations_create_exactly_one_client(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    request = RegistrationRequest(
        redirect_uris=(REDIRECT_A, REDIRECT_B),
        client_name=CLIENT_NAME,
    )
    workers = 8
    barrier = Barrier(workers)
    generated = itertools.count()

    def slow_client_id(_state=None) -> str:
        time.sleep(0.05)
        return f"concurrent-client-{next(generated)}"

    monkeypatch.setattr(oauth_service, "_new_client_id", slow_client_id)
    monkeypatch.setattr(oauth_service, "audit", lambda *args, **kwargs: None)

    def register():
        barrier.wait()
        return oauth_service.register_dynamic_client(request)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda _: register(), range(workers)))

    assert len({result.client.client_id for result in results}) == 1
    assert sum(not result.reused for result in results) == 1
    assert sum(result.reused for result in results) == workers - 1
    assert len(oauth_state().clients) == 1
    assert next(generated) == 1


def test_reuse_audit_records_only_bounded_registration_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        oauth_service,
        "audit",
        lambda event, **fields: events.append((event, fields)),
    )
    request = RegistrationRequest(
        redirect_uris=(REDIRECT_A, REDIRECT_B),
        client_name=CLIENT_NAME,
    )

    oauth_service.register_dynamic_client(request)
    oauth_service.register_dynamic_client(request)

    event, fields = events[-1]
    assert event == "oauth_client_reused"
    assert set(fields) == {"client_id", "approved", "redirect_uri_count"}
    assert fields["approved"] is False
    assert fields["redirect_uri_count"] == 2
