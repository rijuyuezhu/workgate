"""Pinned provider-profile contracts adapted to the private Chat repository."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from workgate.hosted_chat.credential_cipher import (
    CredentialCipher,
    CredentialUnavailable,
)

from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.provider_profiles import (
    ChannelConflict,
    ProviderRepository,
    validate_connection,
)
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def profiles(tmp_path):
    db = SQLiteDatabase(tmp_path)
    return ProviderRepository(db, CredentialCipher(db))


def body(**changes):
    return {
        "provider": "openai",
        "base_url": "https://models.invalid/v1",
        "models": ["a", "b", "a"],
        **changes,
    }


@pytest.mark.parametrize(
    "kind", ["openai", "anthropic", "google", "openai-responses"]
)
def test_named_channels_independent_encrypted_and_persistent(profiles, kind):
    for name, secret in (("模型一", "secret-one"), ("模型二", "secret-two")):
        public = profiles.save(
            body(
                name=name,
                provider=kind,
                api_key=secret,
                model_contexts={"a": 8192},
                model_sampling={"a": {"temperature": 0}},
            )
        )
        assert secret not in json.dumps(public) and "api_key" not in json.dumps(
            public
        )
        assert profiles.profile(name)["api_key"] == secret
        assert profiles.profile(name)["models"] == ["a", "b"]
    with profiles.database.connect() as db:
        rows = db.execute(
            "SELECT value FROM settings WHERE key LIKE 'model.channel.%'"
        ).fetchall()
        for row in rows:
            value = json.loads(row[0])
            assert "api_key" not in value
            assert "secret-one" not in row[0] and "secret-two" not in row[0]
            assert value["api_key_ciphertext"]
    root = profiles.database.root.parent
    reopened_db = SQLiteDatabase(root)
    reopened = ProviderRepository(reopened_db, CredentialCipher(reopened_db))
    assert reopened.profile("模型一")["api_key"] == "secret-one"
    assert reopened.profile("模型二")["api_key"] == "secret-two"
    for key in (None, "", "   "):
        reopened.save(
            body(name="模型一", provider=kind, api_key=key, models=["a"])
        )
        assert reopened.profile("模型一")["api_key"] == "secret-one"
        assert reopened.profile("模型一")["model_sampling"] == {
            "a": {"temperature": 0}
        }
    reopened.save(
        body(name="模型一", provider=kind, api_key="replacement", models=["b"])
    )
    assert reopened.profile("模型一")["model_sampling"] == {}
    assert reopened.profile("模型一")["api_key"] == "replacement"


def test_endpoint_change_does_not_reuse_credentials(profiles):
    profiles.save(body(name="local", api_key="old-secret"))
    before = profiles.public_profiles()
    with pytest.raises(ValueError, match="changed endpoint"):
        profiles.save(body(name="local", base_url="https://other.invalid/v1"))
    assert profiles.public_profiles() == before
    assert profiles.profile("local")["api_key"] == "old-secret"
    profiles.save(
        body(
            name="local",
            base_url="https://other.invalid/v1",
            api_key="new-secret",
        )
    )
    assert profiles.profile("local")["api_key"] == "new-secret"
    profiles.save(
        body(
            name="local",
            base_url="http://100.64.1.2:8080/v1",
            allow_private_http=True,
            keyless=True,
        )
    )
    assert profiles.profile("local")["api_key"] == ""
    assert profiles.profile("local")["keyless"] is True
    profiles.save(
        body(
            name="local",
            base_url="http://100.64.1.2:8080/v1",
            allow_private_http=True,
        )
    )
    assert profiles.profile("local")["keyless"] is True


def test_reference_protection_rename_delete_and_recreation(profiles):
    profiles.save(body(name="old", api_key="old-key"))
    profiles.save(body(name="renamed", previous_name="old"))
    with pytest.raises(ValueError, match="Unknown model channel"):
        profiles.profile("old")
    assert profiles.profile("renamed")["api_key"] == "old-key"
    repo = WorkspaceRepository(profiles.database)
    conversation = repo.create_conversation("owner", {"endpoint": "renamed"})
    with pytest.raises(ChannelConflict, match="referenced"):
        profiles.save(body(name="other", previous_name="renamed"))
    with pytest.raises(ChannelConflict) as failure:
        profiles.delete(name="renamed")
    assert failure.value.can_force
    profiles.delete(name="renamed", force=True)
    with pytest.raises(ValueError, match="Unknown model channel"):
        profiles.profile("renamed")
    current = repo.conversation("owner", conversation["conversationId"])
    assert current is not None and current["endpoint"] == "renamed"
    with pytest.raises(ChannelConflict, match="history"):
        profiles.save(body(name="renamed", api_key="new-key"))


@pytest.mark.parametrize(
    "legacy,alias",
    [
        ("openai", " OpenAI-compatible "),
        ("anthropic", "ANTHROPIC"),
        ("google", "Gemini"),
    ],
)
def test_legacy_alias_protected_from_reset(profiles, legacy, alias):
    profiles.save(body(provider=legacy, api_key="secret"))
    assert profiles.profile(alias)["api_key"] == "secret"
    WorkspaceRepository(profiles.database).create_conversation(
        "owner", {"endpoint": alias}
    )
    with pytest.raises(ChannelConflict) as failure:
        profiles.delete(legacy_provider=legacy, force=True)
    assert not failure.value.can_force


def test_legacy_reset_and_default_does_not_read_ambient_key(
    profiles, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    assert profiles.profile("openai")["api_key"] == ""
    profiles.save(body(api_key="explicit"))
    profiles.delete(legacy_provider="openai")
    assert profiles.profile("openai")["api_key"] == ""
    assert profiles.public_profiles()["providers"][0]["source"] == "default"


def test_concurrent_name_collision_and_protocol_change(profiles):
    def save(name):
        try:
            profiles.save(body(name=name, api_key="secret"))
        except ChannelConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["Channel", "channel"])) == [False, True]
    selected = next(
        p["name"]
        for p in profiles.public_profiles()["providers"]
        if p.get("name")
    )
    with pytest.raises(ChannelConflict, match="provider type"):
        profiles.save(body(name=selected, provider="anthropic"))
    assert profiles.profile(selected)["provider"] == "openai"


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "other"},
        {"provider": "openai-responses"},
        {"name": "agents"},
        {"name": " bad"},
        {"name": "bad/"},
        {"name": "abcassistants"},
        {"models": []},
        {"models": [" "]},
        {"models": ["\n"]},
        {"models": [1]},
        {"provider": "google", "models": ["bad/model"]},
        {"model_contexts": {"a": True}},
        {"model_contexts": {"c": 10}},
        {"model_sampling": {"a": {"temperature": 3}}},
        {"model_sampling": {"c": {"temperature": 1}}},
        {"auto_discover": "yes"},
        {"auto_discover": True},
        {"api_key": "bad\nkey"},
        {"api_key": 1},
        {"keyless": True, "api_key": "secret"},
        {"provider": "anthropic", "keyless": True},
        {"previous_name": "agents"},
    ],
)
def test_invalid_profiles_are_not_saved(profiles, changes):
    before = profiles.public_profiles()
    with pytest.raises(ValueError):
        profiles.save(body(**changes))
    assert profiles.public_profiles() == before


@pytest.mark.parametrize(
    "url,allow",
    [
        ("http://example.com", False),
        ("http://8.8.8.8", True),
        ("http://169.254.169.254", True),
        ("http://0.0.0.0", True),
        ("ftp://localhost", True),
        ("https://user:pass@host", False),
        ("https://host:0", False),
        ("https://host:bad", False),
        ("https://host?q=x", False),
        ("https://host/#fragment", False),
        ("https://host\\evil", False),
        ("https://host/a b", False),
        ("https://[not-ip]", False),
        ("https:///path", False),
        ("https://host", "yes"),
    ],
)
def test_invalid_endpoint_syntax(url, allow):
    with pytest.raises(ValueError):
        validate_connection(body(base_url=url, allow_private_http=allow))


@pytest.mark.parametrize(
    "url,allow",
    [
        ("https://example.com/v1/", False),
        ("http://localhost:8000/v1", False),
        ("http://[::1]:8000/v1", False),
        ("http://10.0.0.2/v1", True),
        ("http://server.tailnet/v1", True),
        ("http://[fd00::1]/v1", True),
    ],
)
def test_approved_endpoint_syntax(url, allow):
    assert validate_connection(body(base_url=url, allow_private_http=allow))[
        1
    ] == url.rstrip("/")


def test_swapped_ciphertext_fails_closed(profiles):
    profiles.save(body(name="one", api_key="secret-one"))
    profiles.save(body(name="two", api_key="secret-two"))
    with profiles.database.connect() as db:
        first = json.loads(
            db.execute(
                "SELECT value FROM settings WHERE key='model.channel.one'"
            ).fetchone()[0]
        )
        second = json.loads(
            db.execute(
                "SELECT value FROM settings WHERE key='model.channel.two'"
            ).fetchone()[0]
        )
        second["api_key_ciphertext"] = first["api_key_ciphertext"]
        db.execute(
            "UPDATE settings SET value=? WHERE key='model.channel.two'",
            (json.dumps(second),),
        )
    with pytest.raises(CredentialUnavailable):
        profiles.profile("two")
