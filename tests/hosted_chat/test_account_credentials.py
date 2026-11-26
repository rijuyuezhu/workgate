"""Account storage and rotating refresh contracts from pinned test_webui_model_oauth.

Source: v6.0.3 afb8ef64aac29ac0674444378c111686f042c134.
"""

import asyncio
import json
import time

import httpx
import pytest

from workgate.hosted_chat import account_credentials as accounts
from workgate.hosted_chat import model_oauth
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.provider_profiles import (
    ChannelConflict,
    ProviderRepository,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def repo(tmp_path, monkeypatch):
    database = SQLiteDatabase(tmp_path)

    async def catalog(self, token):
        return {"models": ["model"], "model_contexts": {"model": 32768}}

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "catalog", catalog)
    return ProviderRepository(database, CredentialCipher(database))


def credentials(**changes):
    return {
        "client_id": "oaiapp_mock",
        "subject": "account",
        "scope": model_oauth.SCOPE,
        "access_token": "mock-access",
        "refresh_token": "mock-refresh",
        "id_token": "mock-id",
        "expires_at": time.time() - 1,
        **changes,
    }


async def test_storage_binding_and_registration(repo):
    data = credentials()
    await accounts.save_account(repo, "chatgpt", "Account", data)
    saved = repo.profile("Account")
    assert accounts.unseal(repo, saved) == data
    public = repo.public_profiles()
    assert any(
        row.get("name") == "Account"
        and row["configured"]
        and row["auth_method"] == "chatgpt"
        for row in public["providers"]
    )
    assert all(
        data[key] not in json.dumps(public)
        for key in ("access_token", "refresh_token", "id_token")
    )
    assert "oauth_credentials" not in json.dumps(public)
    with repo.database.read_snapshot() as db:
        raw = db.execute(
            "SELECT value FROM settings WHERE key='model.channel.Account'"
        ).fetchone()[0]
    assert all(
        data[key] not in raw
        for key in ("access_token", "refresh_token", "id_token")
    )
    assert accounts.host_id(repo) == accounts.host_id(
        ProviderRepository(repo.database, CredentialCipher(repo.database))
    )
    with pytest.raises(model_oauth.ReauthorizationRequired):
        accounts.unseal(repo, {**saved, "oauth_id": "another"})
    with pytest.raises(ValueError, match="original API endpoint"):
        accounts.check_binding({**saved, "base_url": "https://other.invalid"})
    with pytest.raises(ChannelConflict, match="already exists"):
        await accounts.save_account(repo, "chatgpt", "account", data)
    with pytest.raises(ChannelConflict, match="already connected"):
        await accounts.save_account(repo, "chatgpt", "Other", data)
    with pytest.raises(ValueError, match="original account"):
        await accounts.save_account(
            repo,
            "chatgpt",
            "Account",
            credentials(subject="different"),
            original=saved,
        )
    await accounts.save_account(
        repo,
        "chatgpt",
        "Account",
        credentials(expires_at=time.time() + 3600),
        original=saved,
    )
    with pytest.raises(model_oauth.ReauthorizationRequired):
        await accounts.access_token(repo, "Account", saved["oauth_id"])
    with pytest.raises(ChannelConflict, match="changed"):
        await accounts.save_account(
            repo, "chatgpt", "Account", data, original=saved
        )


async def test_parallel_refresh_and_rejected_token(repo, monkeypatch):
    await accounts.save_account(repo, "chatgpt", "Account", credentials())
    saved = repo.profile("Account")
    identity = saved["oauth_id"]
    calls = []

    async def refresh(self, previous):
        calls.append(previous["refresh_token"])
        await asyncio.sleep(0.05)
        return {
            **previous,
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", refresh)
    repos = [
        ProviderRepository(repo.database, CredentialCipher(repo.database))
        for _ in range(3)
    ]
    assert (
        await asyncio.gather(
            *(accounts.access_token(r, "Account", identity) for r in repos)
        )
        == ["new-access"] * 3
    )
    assert calls == ["mock-refresh"]
    assert (
        accounts.unseal(repo, repo.profile("Account"))["refresh_token"]
        == "new-refresh"
    )
    assert (
        await accounts.access_token(
            repo, "Account", identity, rejected="mock-access"
        )
        == "new-access"
    )
    assert calls == ["mock-refresh"]


async def test_rename_retains_credential_binding_and_catalog(repo):
    await accounts.save_account(
        repo, "chatgpt", "Account", credentials(expires_at=time.time() + 3600)
    )
    saved = repo.profile("Account")
    body = {
        "name": "Renamed",
        "previous_name": "Account",
        "provider": "openai-responses",
        "base_url": model_oauth.RESOURCE,
        "models": ["model"],
        "auto_discover": True,
        "model_contexts": {"model": 4096},
    }
    with pytest.raises(ValueError, match="original API endpoint"):
        accounts.update_account(
            repo, {**body, "base_url": "https://wrong.invalid"}
        )
    accounts.update_account(repo, body)
    current = repo.profile("Renamed")
    assert current["oauth_id"] == saved["oauth_id"]
    assert current["oauth_credentials"] == saved["oauth_credentials"]
    assert current["model_contexts"] == {"model": 4096}
    assert (
        await accounts.access_token(repo, "Account", saved["oauth_id"])
        == "mock-access"
    )
    with pytest.raises(ValueError, match="Unknown model channel"):
        repo.profile("Account")


async def test_earliest_refresh_and_explicit_reauthorization(repo, monkeypatch):
    await accounts.save_account(
        repo,
        "chatgpt",
        "Account",
        credentials(
            expires_at=time.time() + 30, earliest_refresh_at=time.time() + 180
        ),
    )
    value = repo.profile("Account")
    assert (
        await accounts.access_token(repo, "Account", value["oauth_id"])
        == "mock-access"
    )
    with pytest.raises(ValueError, match="cannot be refreshed yet"):
        await accounts.access_token(
            repo, "Account", value["oauth_id"], rejected="mock-access"
        )
    await accounts.save_account(
        repo, "chatgpt", "Account", credentials(), original=value
    )
    value = repo.profile("Account")

    async def denied(self, previous):
        raise model_oauth.ReauthorizationRequired("private provider error")

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", denied)
    with pytest.raises(model_oauth.ReauthorizationRequired):
        await accounts.access_token(repo, "Account", value["oauth_id"])
    assert (
        repo.profile("Account")["oauth_error"]
        == "Account authorization expired; sign in again"
    )
    with pytest.raises(model_oauth.ReauthorizationRequired):
        await accounts.access_token(repo, "Account", value["oauth_id"])


async def test_expired_lease_does_not_repeat_rotation(repo, monkeypatch):
    await accounts.save_account(repo, "chatgpt", "Account", credentials())
    value = repo.profile("Account")
    key = "model.oauth.refresh." + value["oauth_id"]
    with repo.database.connect() as db:
        accounts._store_value(
            db, key, {"owner": "crashed", "until": time.time() - 1}
        )

    async def unexpected(*args):
        pytest.fail("must not replay uncertain rotation")

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", unexpected)
    with pytest.raises(
        model_oauth.ReauthorizationRequired, match="interrupted"
    ):
        await accounts.access_token(repo, "Account", value["oauth_id"])
    assert repo.profile("Account")["oauth_error"] == accounts._REAUTHORIZE
    with repo.database.read_snapshot() as db:
        assert accounts._lease_row(db, key) is None


async def test_refresh_cancel_and_delete_do_not_restore_credentials(
    repo, monkeypatch
):
    await accounts.save_account(repo, "chatgpt", "Account", credentials())
    value = repo.profile("Account")
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh(self, previous):
        started.set()
        await release.wait()
        return {
            **previous,
            "access_token": "new-access",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", refresh)
    task = asyncio.create_task(
        accounts.access_token(repo, "Account", value["oauth_id"])
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.profile("Account")["oauth_error"] == accounts._REAUTHORIZE
    await accounts.save_account(
        repo,
        "chatgpt",
        "Account",
        credentials(),
        original=repo.profile("Account"),
    )
    value = repo.profile("Account")
    started.clear()
    task = asyncio.create_task(
        accounts.access_token(repo, "Account", value["oauth_id"])
    )
    await started.wait()
    repo.delete(name="Account")
    release.set()
    with pytest.raises(model_oauth.ReauthorizationRequired, match="changed"):
        await task
    with pytest.raises(ValueError, match="Unknown"):
        repo.profile("Account")


async def test_catalog_rejects_old_access_token(repo, monkeypatch):
    await accounts.save_account(
        repo, "chatgpt", "Account", credentials(expires_at=time.time() + 3600)
    )
    value = repo.profile("Account")
    calls = []

    async def catalog(self, token):
        calls.append(token)
        if token == "mock-access":
            raise httpx.HTTPStatusError(
                "denied",
                request=httpx.Request("GET", model_oauth.RESOURCE),
                response=httpx.Response(401),
            )
        return {"models": ["new"], "model_contexts": {}}

    async def refresh(self, previous):
        return {
            **previous,
            "access_token": "new-access",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "catalog", catalog)
    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", refresh)
    assert await accounts.catalog_for(repo, value) == {
        "models": ["new"],
        "model_contexts": {},
    }
    assert calls == ["mock-access", "new-access"]


async def test_account_catalog_refresh_reuses_single_discovery_service(
    repo, monkeypatch
):
    from workgate.hosted_chat import model_discovery

    await accounts.save_account(repo, "chatgpt", "Account", credentials())
    before = repo.profile("Account")
    body = {
        "name": "Account",
        "provider": "openai-responses",
        "base_url": model_oauth.RESOURCE,
        "models": ["model"],
        "auto_discover": True,
        "model_contexts": {"model": 4096},
        "model_sampling": {"model": {"temperature": 0.3}},
    }
    accounts.update_account(repo, body)
    before = repo.profile("Account")
    calls = []

    async def refresh(self, previous):
        return {
            **previous,
            "access_token": "rotated",
            "refresh_token": "rotated-refresh",
            "expires_at": time.time() + 3600,
        }

    async def catalog(self, token):
        calls.append(token)
        return {
            "models": ["model", "new"],
            "model_contexts": {"model": 65536, "new": 32768},
        }

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", refresh)
    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "catalog", catalog)
    # Account activity is explicitly disabled unless its runtime provides the
    # accepted callback. No hidden token request or background service.
    await model_discovery.refresh_model_channels(repo)
    assert not calls
    with pytest.raises(ValueError, match="account authorization"):
        await model_discovery.refresh_model_channel(repo, "Account")
    await model_discovery.refresh_model_channels(
        repo, account_refresh=accounts.refresh_account_snapshot
    )
    current = repo.profile("Account")
    assert calls == ["rotated"]
    assert current["oauth_credentials"] != before["oauth_credentials"]
    assert current["models"] == ["model", "new"]
    assert current["model_contexts"] == {"model": 4096, "new": 32768}
    assert current["model_sampling"] == {"model": {"temperature": 0.3}}

    async def failed(self, token):
        raise httpx.ConnectError("private provider diagnostic")

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "catalog", failed)
    await model_discovery.refresh_model_channel(
        repo, "Account", account_refresh=accounts.refresh_account_snapshot
    )
    retained = repo.profile("Account")
    assert retained["model_sync_at"] == current["model_sync_at"]
    assert retained["models"] == current["models"]
    assert retained["oauth_credentials"] == current["oauth_credentials"]
    assert retained["model_sync_error"] == model_discovery._SYNC_ERROR


@pytest.mark.parametrize("change", ["edit", "delete", "reauthorize"])
async def test_account_catalog_cas_rejects_stale_result(
    repo, monkeypatch, change
):
    await accounts.save_account(
        repo, "chatgpt", "Account", credentials(expires_at=time.time() + 3600)
    )
    before = repo.profile("Account")
    with repo.database.read_snapshot() as db:
        raw = db.execute(
            "SELECT value FROM settings WHERE key='model.channel.Account'"
        ).fetchone()[0]

    async def catalog(self, token):
        if change == "edit":
            accounts.update_account(
                repo,
                {
                    "name": "Account",
                    "provider": "openai-responses",
                    "base_url": model_oauth.RESOURCE,
                    "models": ["manual"],
                    "auto_discover": False,
                },
            )
        elif change == "delete":
            repo.delete(name="Account")
        else:

            async def replacement_catalog(self, token):
                return {"models": ["reauthorized"], "model_contexts": {}}

            monkeypatch.setattr(
                model_oauth.ChatGPTAuthorization, "catalog", replacement_catalog
            )
            await accounts.save_account(
                repo,
                "chatgpt",
                "Account",
                credentials(expires_at=time.time() + 3600),
                original=before,
            )
        return {"models": ["stale"], "model_contexts": {}}

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "catalog", catalog)
    assert not await accounts.refresh_account_snapshot(
        repo, "model.channel.Account", raw
    )
    if change == "delete":
        with pytest.raises(ValueError, match="Unknown"):
            repo.profile("Account")
    else:
        assert repo.profile("Account")["models"] == [
            "manual" if change == "edit" else "reauthorized"
        ]


async def test_active_and_expired_refresh_lease_fences_completion(
    repo, monkeypatch
):
    await accounts.save_account(repo, "chatgpt", "Account", credentials())
    value = repo.profile("Account")
    lease = "model.oauth.refresh." + value["oauth_id"]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def refresh(self, previous):
        entered.set()
        await release.wait()
        return {
            **previous,
            "access_token": "late-token",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", refresh)
    task = asyncio.create_task(
        accounts.access_token(repo, "Account", value["oauth_id"])
    )
    try:
        await entered.wait()
        with pytest.raises(ChannelConflict, match="busy"):
            await accounts.save_account(
                repo, "chatgpt", "Account", credentials(), original=value
            )
        monkeypatch.setattr(accounts, "_REFRESH_WAIT_S", 0)
        with pytest.raises(ValueError, match="busy"):
            await accounts.access_token(repo, "Account", value["oauth_id"])
        with repo.database.connect() as db:
            active = accounts._lease_row(db, lease)
            assert active is not None
            accounts._store_value(
                db, lease, {**active, "until": time.time() - 1}
            )
        with pytest.raises(
            model_oauth.ReauthorizationRequired, match="interrupted"
        ):
            await accounts.access_token(repo, "Account", value["oauth_id"])
        release.set()
        with pytest.raises(
            model_oauth.ReauthorizationRequired, match="changed"
        ):
            await task
        current = repo.profile("Account")
        assert current["oauth_credentials"] == value["oauth_credentials"]
        assert current["oauth_error"] == accounts._REAUTHORIZE
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
