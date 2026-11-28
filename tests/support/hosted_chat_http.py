"""Shared isolated Control HTTP fixtures for Hosted Chat transport tests."""

from typing import Literal

from workgate.config.settings import Settings
from workgate.control.runtime import build_control_runtime
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.provider_profiles import ProviderRepository


def runtime_for(
    tmp_path,
    *,
    enabled=True,
    auth_mode: Literal["none", "oauth"] = "none",
    ui_enabled=True,
):
    return build_control_runtime(
        Settings(
            default_workdir=tmp_path,
            state_dir=tmp_path / ".state",
            auth_mode=auth_mode,
            auth_bypass_localhost=False,
            hosted_chat_enabled=enabled,
            ui_enabled=ui_enabled,
            agent_bridge_enabled=False,
        )
    )


def provider(runtime):
    db = SQLiteDatabase(runtime.config.state_dir)
    ProviderRepository(db, CredentialCipher(db)).save(
        {
            "name": "test-provider",
            "provider": "openai",
            "models": ["test-model"],
            "base_url": "https://models.invalid/v1",
            "api_key": "test-secret",
        }
    )
    return db
