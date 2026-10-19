"""Authenticated config Apply acknowledges before an actual Child restart."""

import argparse
import json
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request
from starlette.routing import Route

from workgate.config.editing import revision_for
from workgate.config.inspection import (
    capture_startup_config,
    current_startup_config,
)
from workgate.config.roles import CONTROL_SETTING_NAMES
from workgate.config.settings import load_settings
from workgate.control.http import config_inspection
from workgate.control.http.config_inspection import config_inspection_routes
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.oauth.core.scopes import default_scope


async def invoke(payload, *, scopes=None, executor_id="", runtime=None):
    scopes = default_scope() if scopes is None else scopes
    token = bind_oauth_claims({"scope": scopes})
    encoded = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    try:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/ui/config/apply",
                "query_string": ("executor_id=" + executor_id).encode(),
                "headers": [(b"content-type", b"application/json")],
                "scheme": "https",
                "server": ("test", 443),
                "app": SimpleNamespace(
                    state=SimpleNamespace(control_runtime=runtime)
                ),
            },
            receive,
        )
        route = cast(Route, config_inspection_routes(api_prefix="/api/ui")[2])
        return await route.endpoint(request)
    finally:
        reset_oauth_claims(token)


def startup(tmp_path):
    path = tmp_path / "control.yaml"
    path.write_text("port: 8765\n")
    capture_startup_config(
        "control",
        argparse.Namespace(config=str(path)),
        load_settings(path, setting_names=CONTROL_SETTING_NAMES),
    )
    snap = current_startup_config("control")
    assert snap is not None
    return path, snap


@pytest.mark.asyncio
async def test_control_apply_acks_before_local_parent_restart(
    tmp_path, monkeypatch
):
    path, snap = startup(tmp_path)
    path.write_text("port: 9217\n")
    calls = []
    monkeypatch.setattr(config_inspection, "restart_status", lambda: "healthy")
    monkeypatch.setattr(
        config_inspection,
        "request_restart",
        lambda revision: calls.append(("restart", revision)),
    )
    response = await invoke({"revision": revision_for(snap)})
    assert response.status_code == 202
    assert not calls
    assert response.background is not None
    await response.background()
    assert calls == [("restart", revision_for(snap))]
    assert snap.settings.port == 8765


@pytest.mark.asyncio
async def test_control_apply_rejects_unmanaged_stale_invalid_or_unchanged(
    tmp_path, monkeypatch
):
    path, snap = startup(tmp_path)
    monkeypatch.setattr(
        config_inspection, "restart_status", lambda: "unmanaged"
    )
    assert (await invoke({"revision": revision_for(snap)})).status_code == 409
    monkeypatch.setattr(config_inspection, "restart_status", lambda: "healthy")
    assert (await invoke({"revision": revision_for(snap)})).status_code == 409
    path.write_text("port: 9011\n")
    assert (await invoke({"revision": "0" * 64})).status_code == 409
    path.write_text("port: 90000\n")
    response = await invoke({"revision": revision_for(snap)})
    assert response.status_code == 400
    assert b"90000" not in response.body
    assert (await invoke({"revision": 22})).status_code == 400
    # Reject an oversized body before buffering or parsing its content.
    assert (await invoke({"revision": "a" * 2048})).status_code == 400


@pytest.mark.asyncio
async def test_apply_requires_existing_write_scope(tmp_path, monkeypatch):
    path, snap = startup(tmp_path)
    path.write_text("port: 9217\n")
    monkeypatch.setattr(config_inspection, "restart_status", lambda: "healthy")
    response = await invoke(
        {"revision": revision_for(snap)}, scopes="shell:read"
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_remote_apply_denies_offline_and_forwards_only_authorized(
    monkeypatch,
):
    calls = []

    class Transport:
        online = False
        result = {"status": "accepted"}

        async def is_online(self, executor_id):
            return self.online

        async def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(ok=True, result=self.result)

    transport = Transport()
    runtime = SimpleNamespace(
        control_state=SimpleNamespace(
            snapshot_executors=lambda: {
                "exec_1": SimpleNamespace(revoked_at=None)
            }
        ),
        executor_transport=transport,
    )
    payload = {"revision": "a" * 64}
    denied = await invoke(
        payload, scopes="shell:write", executor_id="exec_1", runtime=runtime
    )
    assert denied.status_code == 403 and not calls
    offline = await invoke(payload, executor_id="exec_1", runtime=runtime)
    assert offline.status_code == 409 and not calls
    transport.online = True
    accepted = await invoke(payload, executor_id="exec_1", runtime=runtime)
    assert accepted.status_code == 202
    assert calls == [
        (("exec_1", "ui.config.apply", payload), {"timeout_s": 12})
    ]
