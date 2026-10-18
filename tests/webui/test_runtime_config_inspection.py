"""Existing WebUI read permissions protect live runtime configuration."""

import argparse
import json
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request
from starlette.routing import Route

from workgate.config.inspection import capture_startup_config
from workgate.config.roles import CONTROL_SETTING_NAMES, EXECUTOR_SETTING_NAMES
from workgate.config.settings import load_settings
from workgate.control.http.config_inspection import config_inspection_routes
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.oauth.core.scopes import (
    SCOPE_EXECUTOR_USE,
    SCOPE_SHELL_READ,
    default_scope,
)


def _capture(role, config_file):
    selected = (
        CONTROL_SETTING_NAMES if role == "control" else EXECUTOR_SETTING_NAMES
    )
    args = argparse.Namespace(config=str(config_file))
    capture_startup_config(
        role, args, load_settings(config_file, setting_names=selected)
    )


async def _request(*, scope=None, executor_id="", runtime=None):
    token = bind_oauth_claims(None if scope is None else {"scope": scope})
    try:
        app = SimpleNamespace(state=SimpleNamespace(control_runtime=runtime))
        req = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/ui/config",
                "query_string": ("executor_id=" + executor_id).encode()
                if executor_id
                else b"",
                "headers": [],
                "scheme": "https",
                "server": ("example.test", 443),
                "app": app,
            }
        )
        route = cast(Route, config_inspection_routes(api_prefix="/api/ui")[0])
        return await route.endpoint(req)
    finally:
        reset_oauth_claims(token)


@pytest.mark.asyncio
async def test_existing_read_scope_denies_unrelated_grants(tmp_path):
    file = tmp_path / "control.yaml"
    file.write_text("oauth_admin_pin: raw-secret\n")
    _capture("control", file)
    for scopes in ("", "audit:read", "executor:use", "shell:write"):
        response = await _request(scope=scopes)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
        assert b"raw-secret" not in response.body


@pytest.mark.asyncio
async def test_regular_webui_grant_reads_actual_credentials_no_store(tmp_path):
    file = tmp_path / "control.yaml"
    file.write_text("oauth_admin_pin: raw-secret\nlog_level: info\n")
    _capture("control", file)
    response = await _request(scope=default_scope())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    payload = json.loads(response.body)["data"]
    rows = {row["name"]: row for row in payload["fields"]}
    assert rows["oauth_admin_pin"]["effective_value"] == "raw-secret"
    assert rows["oauth_admin_pin"]["source"] == "file"


@pytest.mark.asyncio
async def test_executor_offline_discloses_no_stale_secret(tmp_path):
    file = tmp_path / "executor.json"
    file.write_text('{"max_jobs": 42}')
    _capture("executor", file)

    class FakeTransport:
        async def is_online(self, executor_id):
            assert executor_id == "exec_test"
            return False

    runtime = SimpleNamespace(
        control_state=SimpleNamespace(
            snapshot_executors=lambda: {
                "exec_test": SimpleNamespace(revoked_at=None),
            }
        ),
        executor_transport=FakeTransport(),
    )
    response = await _request(
        scope=default_scope(), executor_id="exec_test", runtime=runtime
    )
    assert response.status_code == 200
    assert json.loads(response.body)["data"] == {
        "role": "executor",
        "executor_id": "exec_test",
        "status": "offline",
        "fields": [],
    }


@pytest.mark.asyncio
async def test_executor_online_uses_one_authorized_rpc(tmp_path):
    file = tmp_path / "executor.json"
    file.write_text('{"max_jobs": 42}')
    _capture("executor", file)
    calls = []

    class FakeTransport:
        async def is_online(self, executor_id):
            return True

        async def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(
                ok=True,
                result={
                    "status": "running",
                    "fields": [{"name": "max_jobs", "effective_value": 42}],
                },
            )

    runtime = SimpleNamespace(
        control_state=SimpleNamespace(
            snapshot_executors=lambda: {
                "exec_test": SimpleNamespace(revoked_at=None),
            }
        ),
        executor_transport=FakeTransport(),
    )
    response = await _request(
        scope=default_scope(), executor_id="exec_test", runtime=runtime
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == ("exec_test", "ui.config.inspect", {})
    assert calls[0][1] == {"timeout_s": 8}
    assert json.loads(response.body)["data"]["executor_id"] == "exec_test"


@pytest.mark.asyncio
async def test_executor_requires_existing_executor_use_and_read_grants(
    tmp_path,
):
    file = tmp_path / "executor.json"
    file.write_text('{"max_jobs": 42}')
    _capture("executor", file)
    # Authorization is checked before querying executor state or attempting RPC.
    for scopes in (SCOPE_SHELL_READ, SCOPE_EXECUTOR_USE, "audit:read"):
        response = await _request(scope=scopes, executor_id="exec_test")
        assert response.status_code == 403
    assert "config:admin" not in default_scope().split()


@pytest.mark.asyncio
async def test_auth_mode_none_keeps_existing_bypass_semantics(tmp_path):
    # Middleware grants the no-auth mode a claims=None request, just as other
    # WebUI read operations do; this route must not create its own user tiers.
    file = tmp_path / "control.yaml"
    file.write_text("port: 8766\n")
    _capture("control", file)
    response = await _request(scope=None)
    assert response.status_code == 200
