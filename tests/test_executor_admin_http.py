from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.http.app import build_http_app
from workgate.control.runtime import build_control_runtime
from workgate.protocol.executor import (
    ExecutorHelloRequest,
    ExecutorRuntimeSummary,
)
from workgate.ui.security import (
    UI_LOCAL_TOKEN_HEADER,
    get_or_create_ui_local_token,
)


def _configure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://control.test")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "false")
    clear_settings_cache()


def _hello_payload() -> dict[str, object]:
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(
            workgate_version="test",
            platform="linux",
            build="fixture",
        ),
        sessions=(),
        shells=(),
        jobs=(),
    ).model_dump(mode="json")


def test_pair_verification_shell_is_public_but_owner_api_is_authenticated(
    tmp_path: Path, monkeypatch
) -> None:
    _configure(monkeypatch, tmp_path)
    settings = get_settings()
    runtime = build_control_runtime(settings)
    app = build_http_app(runtime=runtime)

    try:
        with TestClient(
            app,
            base_url="https://control.test",
            client=("203.0.113.10", 50000),
        ) as client:
            shell = client.get("/pair")
            assert shell.status_code == 200
            assert "Workgate" in shell.text

            protected = client.get("/api/ui/executors")
            assert protected.status_code == 401
    finally:
        clear_settings_cache()


def test_owner_can_approve_list_rename_and_revoke_final_executor(
    tmp_path: Path, monkeypatch
) -> None:
    _configure(monkeypatch, tmp_path)
    settings = get_settings()
    runtime = build_control_runtime(settings)
    app = build_http_app(runtime=runtime)

    try:
        with TestClient(
            app,
            base_url="https://control.test",
            client=("127.0.0.1", 50000),
        ) as client:
            token = get_or_create_ui_local_token()
            owner_headers = {UI_LOCAL_TOKEN_HEADER: token}

            started = client.post(
                "/executor/v1/pair/start",
                json={
                    "requested_name": "request-name",
                    "metadata": {
                        "hostname": "laptop.local",
                        "platform": "linux",
                        "build": "fixture",
                    },
                },
            )
            assert started.status_code == 200
            pair = started.json()
            assert pair["verification_uri"] == "https://control.test/pair"

            lookup = client.get(
                "/api/ui/pair",
                params={"code": pair["user_code"].lower()},
                headers=owner_headers,
            )
            assert lookup.status_code == 200
            lookup_data = lookup.json()["data"]
            assert lookup_data["user_code"] == pair["user_code"]
            assert lookup_data["requested_name"] == "request-name"
            assert lookup_data["metadata"]["hostname"] == "laptop.local"
            assert "device_code" not in lookup.text

            approved = client.post(
                "/api/ui/pair",
                headers=owner_headers,
                json={
                    "user_code": pair["user_code"],
                    "decision": "approve",
                    "name": "Laptop",
                },
            )
            assert approved.status_code == 200
            approved_data = approved.json()["data"]
            executor_id = approved_data["executor_id"]
            assert approved_data["name"] == "Laptop"
            assert "credential" not in approved.text
            assert pair["device_code"] not in approved.text

            delivered = client.post(
                "/executor/v1/pair/poll",
                json={"device_code": pair["device_code"]},
            )
            assert delivered.status_code == 200
            credential = delivered.json()["credential"]
            repeated = client.post(
                "/executor/v1/pair/poll",
                json={"device_code": pair["device_code"]},
            )
            assert repeated.status_code == 202

            listing = client.get("/api/ui/executors", headers=owner_headers)
            assert listing.status_code == 200
            rows = listing.json()["data"]["executors"]
            assert rows == [
                {
                    "executor_id": executor_id,
                    "name": "Laptop",
                    "created_at": rows[0]["created_at"],
                    "revoked_at": None,
                    "online": False,
                    "last_seen_at": None,
                    "runtime": None,
                }
            ]
            assert credential not in listing.text
            assert "credential_verifier" not in listing.text

            hello = client.post(
                "/executor/v1/hello",
                headers={"Authorization": f"Bearer {credential}"},
                json=_hello_payload(),
            )
            assert hello.status_code == 200

            delivery_cleared = client.post(
                "/executor/v1/pair/poll",
                json={"device_code": pair["device_code"]},
            )
            assert delivery_cleared.status_code == 410

            online = client.get("/api/ui/executors", headers=owner_headers)
            row = online.json()["data"]["executors"][0]
            assert row["online"] is True
            assert row["last_seen_at"] is not None
            assert row["runtime"] == {
                "workgate_version": "test",
                "build": "fixture",
                "platform": "linux",
            }

            renamed = client.post(
                "/api/ui/executors/rename",
                headers=owner_headers,
                json={"executor_id": executor_id, "name": "Desk laptop"},
            )
            assert renamed.status_code == 200
            assert renamed.json()["data"]["name"] == "Desk laptop"

            revoked = client.post(
                "/api/ui/executors/revoke",
                headers=owner_headers,
                json={"executor_id": executor_id},
            )
            assert revoked.status_code == 200
            revoked_data = revoked.json()["data"]
            assert revoked_data["revoked_at"] is not None
            assert revoked_data["online"] is False

            rejected = client.post(
                "/executor/v1/heartbeat",
                headers={"Authorization": f"Bearer {credential}"},
            )
            assert rejected.status_code == 403
    finally:
        clear_settings_cache()
