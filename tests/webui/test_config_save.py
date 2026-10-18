"""Config save HTTP privilege, Control CAS, and remote Executor transport."""

import argparse
import json
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request
from starlette.routing import Route

from workgate.config.inspection import (
    capture_startup_config,
    current_startup_config,
)
from workgate.config.roles import CONTROL_SETTING_NAMES
from workgate.config.settings import load_settings
from workgate.control.http.config_inspection import config_inspection_routes
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.oauth.core.scopes import default_scope

DEFAULT_SCOPES = default_scope()


async def post(payload, *, scopes=DEFAULT_SCOPES, executor_id="", runtime=None):
    body = json.dumps(payload).encode("utf-8")
    token = bind_oauth_claims(None if scopes is None else {"scope": scopes})
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    try:
        app = SimpleNamespace(state=SimpleNamespace(control_runtime=runtime))
        req = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/ui/config",
                "query_string": ("executor_id=" + executor_id).encode(),
                "headers": [(b"content-type", b"application/json")],
                "scheme": "https",
                "server": ("test", 443),
                "app": app,
            },
            receive,
        )
        route = cast(Route, config_inspection_routes(api_prefix="/api/ui")[1])
        return await route.endpoint(req)
    finally:
        reset_oauth_claims(token)


@pytest.mark.asyncio
async def test_control_direct_save_without_executor(tmp_path):
    path = tmp_path / "control.yaml"
    path.write_text("port: 8765\n")
    capture_startup_config(
        "control",
        argparse.Namespace(config=str(path)),
        load_settings(path, setting_names=CONTROL_SETTING_NAMES),
    )
    snap = current_startup_config("control")
    assert snap is not None
    from workgate.config.editing import revision_for

    payload = {
        "revision": revision_for(snap),
        "changes": {"port": {"op": "set", "value": 8978}},
    }
    result = await post(payload)
    assert result.status_code == 200, result.body
    assert json.loads(result.body)["data"]["revision"]
    assert path.read_text() == "port: 8978\n"
    assert snap.settings.port == 8765
    assert (tmp_path / "control.yaml.bak").read_text() == "port: 8765\n"
    again = await post(payload)
    assert again.status_code == 409
    assert "RevisionConflict" in again.body.decode()


@pytest.mark.asyncio
async def test_write_scope_required_before_file_access(tmp_path):
    path = tmp_path / "control.yaml"
    path.write_text("port: 8765\n")
    capture_startup_config(
        "control",
        argparse.Namespace(config=str(path)),
        load_settings(path, setting_names=CONTROL_SETTING_NAMES),
    )
    payload = {
        "revision": "0" * 64,
        "changes": {"port": {"op": "set", "value": 8978}},
    }
    for scope in ("shell:read", "executor:use shell:read", "audit:read"):
        result = await post(payload, scopes=scope)
        assert result.status_code == 403
        assert b"8978" not in result.body
    assert path.read_text() == "port: 8765\n"


@pytest.mark.asyncio
async def test_executor_online_forwards_only_authorized_validated_save():
    calls = []

    class Transport:
        async def is_online(self, executor_id):
            return True

        async def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(ok=True, result={"revision": "a" * 64})

    runtime = SimpleNamespace(
        control_state=SimpleNamespace(
            snapshot_executors=lambda: {
                "exec_1": SimpleNamespace(revoked_at=None)
            }
        ),
        executor_transport=Transport(),
    )
    payload = {
        "revision": "0" * 64,
        "changes": {"max_jobs": {"op": "set", "value": 21}},
    }
    denied = await post(
        payload, scopes="shell:write", executor_id="exec_1", runtime=runtime
    )
    assert denied.status_code == 403 and not calls
    accepted = await post(payload, executor_id="exec_1", runtime=runtime)
    assert accepted.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == ("exec_1", "ui.config.save", payload)


@pytest.mark.asyncio
async def test_api_rejects_invalid_payload_without_echo(tmp_path):
    path = tmp_path / "control.yaml"
    path.write_text("port: 8765\n")
    capture_startup_config(
        "control",
        argparse.Namespace(config=str(path)),
        load_settings(path, setting_names=CONTROL_SETTING_NAMES),
    )
    payload = {
        "revision": "a" * 64,
        "changes": {
            "oauth_admin_pin": {"op": "set", "value": "secret-do-not-echo"}
        },
    }
    response = await post(payload)
    assert response.status_code == 409
    assert b"secret-do-not-echo" not in response.body
