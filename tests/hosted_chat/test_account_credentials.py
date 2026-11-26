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
