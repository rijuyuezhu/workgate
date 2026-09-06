import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from workgate.oauth.core import service as oauth_service
from workgate.oauth.core.client_store import (
    CLIENT_STORE_VERSION,
    client_store_path,
    persist_approved_clients,
)
from workgate.oauth.core.models import AuthCode, OAuthClient
from workgate.oauth.core.requests import RegistrationRequest
from workgate.oauth.core.state import (
    OAuthState,
    configure_oauth_state,
    oauth_state,
)
from workgate.persistence import FileStateStore


@pytest.mark.asyncio
async def test_oauth_state_loads_closes_and_rejects_restart(tmp_path) -> None:
    state_dir = tmp_path / "state"
    approved = OAuthClient(
        client_id="approved",
        redirect_uris=["https://client.example/callback"],
        created_at=10,
        approved_at=20,
    )
    persist_approved_clients(
        {approved.client_id: approved}, state_dir=state_dir
    )
    state = OAuthState(state_dir)

    assert state.start() == 1
    assert state.start() == 0
    assert state.clients["approved"].approved_at == 20

    state.codes["code"] = AuthCode(
        code="code",
        client_id="approved",
        redirect_uri="https://client.example/callback",
        scope="shell:read",
        resource="https://workgate.example/mcp",
        code_challenge="challenge",
        code_challenge_method="S256",
    )

    await state.aclose()

    assert state.clients == {}
    assert state.codes == {}
    with pytest.raises(RuntimeError, match="shutting down"):
        state.require_open()
    await state.aclose()
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        state.start()


def test_oauth_state_uses_explicit_state_store_authority(tmp_path) -> None:
    legacy_state_dir = tmp_path / "legacy-state"
    owned_state_dir = tmp_path / "owned-state"
    store = FileStateStore(lambda: owned_state_dir)
    approved = OAuthClient(
        client_id="approved",
        redirect_uris=["https://client.example/callback"],
        created_at=10,
        approved_at=20,
    )
    persist_approved_clients({approved.client_id: approved}, state_store=store)

    state = OAuthState(legacy_state_dir, state_store=store)

    assert state.start() == 1
    assert state.clients == {approved.client_id: approved}
    assert store.layout.oauth_clients_path.exists()
    assert not client_store_path(state_dir=legacy_state_dir).exists()


def test_oauth_state_start_load_is_transactional(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state = OAuthState(state_dir)
    state.clients["existing"] = OAuthClient(
        client_id="existing",
        redirect_uris=["https://existing.example/callback"],
        created_at=1,
    )
    path = client_store_path(state_dir=state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": CLIENT_STORE_VERSION,
                "clients": [
                    {
                        "client_id": "valid",
                        "redirect_uris": ["https://valid.example/callback"],
                        "client_name": None,
                        "created_at": 10,
                        "approved_at": 20,
                    },
                    {
                        "client_id": "invalid",
                        "redirect_uris": ["https://invalid.example/callback"],
                        "client_name": None,
                        "created_at": 30,
                        "approved_at": 29,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError, match="Invalid OAuth client registry contents"
    ):
        state.start()

    assert set(state.clients) == {"existing"}
    state.require_open()


@pytest.mark.asyncio
async def test_queued_client_mutation_rechecks_shutdown_admission(
    tmp_path, monkeypatch
) -> None:
    state = OAuthState(tmp_path / "state")
    assert state.start() == 0
    previous = configure_oauth_state(state)
    admitted = Event()
    original_require_open = state.require_open

    def observe_admission() -> None:
        original_require_open()
        admitted.set()

    monkeypatch.setattr(state, "require_open", observe_admission)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with state.client_lock:
            future = executor.submit(
                oauth_service.register_dynamic_client,
                RegistrationRequest(
                    redirect_uris=("https://client.example/callback",),
                    client_name="queued",
                ),
            )
            assert admitted.wait(timeout=1)
            state.stop_admission()

        with pytest.raises(RuntimeError, match="shutting down"):
            future.result(timeout=1)
        assert state.clients == {}
    finally:
        executor.shutdown(wait=True)
        configure_oauth_state(previous)
        await state.aclose()


def test_oauth_state_compatibility_binding_is_reversible(tmp_path) -> None:
    outer = OAuthState(tmp_path / "outer")
    inner = OAuthState(tmp_path / "inner")
    previous = configure_oauth_state(outer)
    try:
        assert oauth_state() is outer
        assert configure_oauth_state(inner) is outer
        assert oauth_state() is inner
        assert configure_oauth_state(outer) is inner
        assert oauth_state() is outer
    finally:
        configure_oauth_state(previous)
