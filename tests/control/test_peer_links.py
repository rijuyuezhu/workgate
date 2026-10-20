"""On-demand peer connectivity diagnostics between independent Executor runtimes."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue, TypeAdapter

from tests.support.paired import build_paired_http_app
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.control.state import ExecutorTrustRecord
from workgate.executor import peer_probe
from workgate.executor.connection import operation_error_from_exception
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.executor import (
    ExecutorCommand,
    ExecutorResult,
    TrustedOverlay,
)
from workgate.protocol.ids import new_command_id, new_executor_id
from workgate.utils.serialization import to_jsonable

BASE = "https://workgate.example"


@dataclass
class Pair:
    client: TestClient
    control: Any
    source: Any
    destination: Any
    src_id: str
    dst_id: str
    online: dict[str, bool]
    overlays: dict[str, TrustedOverlay | None]
    called: list[tuple[str, str]]


@pytest.fixture
def two_executors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state-control",
        auth_mode="none",
        base_url=BASE,
    )
    app, harness = build_paired_http_app(settings)
    # A *different* ExecutorRuntime and state store; only the test's network
    # path uses loopback since CI cannot bind a real 100.64.0.0/10 interface.
    remote_settings = settings.model_copy(
        update={"state_dir": tmp_path / "state-remote"}
    )
    destination = build_executor_runtime(
        resolve_executor_config(remote_settings),
        enable_control_connection=False,
    )
    dst_id = str(new_executor_id())
    harness.control.control_state.put_executor(
        ExecutorTrustRecord(
            executor_id=dst_id,
            name="remote-executor",
            credential_verifier=executor_credential_verifier(
                new_executor_credential()
            ),
            created_at=1.0,
        )
    )
    peers = {harness.executor_id: harness.executor, dst_id: destination}
    overlays: dict[str, TrustedOverlay | None] = {
        harness.executor_id: TrustedOverlay(
            provider="tailscale",
            tailnet_id="same-tailnet",
            ipv4="100.100.100.10",
        ),
        dst_id: TrustedOverlay(
            provider="tailscale",
            tailnet_id="same-tailnet",
            ipv4="100.100.100.11",
        ),
    }
    online = {harness.executor_id: True, dst_id: True}
    called: list[tuple[str, str]] = []

    # Fake only the test host's routing: packets still cross a real ephemeral
    # TCP listener, while production validators see normal CGNAT IPs.
    current = ContextVar("current_probe_executor", default=harness.executor_id)
    monkeypatch.setattr(
        peer_probe, "detect_tailscale_overlay", lambda: overlays[current.get()]
    )
    original_listen = asyncio.start_server
    original_connect = asyncio.open_connection

    async def loopback_listen(handler, host, port, **kwargs):
        if host in {"100.100.100.10", "100.100.100.11"}:
            host = "127.0.0.1"
        return await original_listen(handler, host, port, **kwargs)

    async def loopback_connect(host, port, **kwargs):
        if host in {"100.100.100.10", "100.100.100.11"}:
            host = "127.0.0.1"
        return await original_connect(host, port, **kwargs)

    monkeypatch.setattr(peer_probe.asyncio, "start_server", loopback_listen)
    monkeypatch.setattr(peer_probe.asyncio, "open_connection", loopback_connect)

    async def is_online(executor_id: str) -> bool:
        return online.get(executor_id, False)

    async def inventory(executor_id: str):
        return SimpleNamespace(overlay=overlays.get(executor_id))

    async def call(
        executor_id: str,
        op: str,
        args: dict | None = None,
        *,
        session_id=None,
        timeout_s=None,
    ) -> ExecutorResult:
        _ = timeout_s
        called.append((executor_id, op))
        command = ExecutorCommand(
            id=new_command_id(),
            op=op,
            session_id=session_id,
            args=args or {},
        )
        stamp = current.set(executor_id)
        try:
            result = await peers[executor_id]._execute_protocol_command(command)
        except Exception as exc:
            return ExecutorResult(
                id=command.id,
                ok=False,
                error=operation_error_from_exception(exc),
            )
        finally:
            current.reset(stamp)
        return ExecutorResult(
            id=command.id,
            ok=True,
            result=TypeAdapter(JsonValue).validate_python(to_jsonable(result)),
        )

    transport = harness.control.executor_transport
    monkeypatch.setattr(transport, "is_online", is_online)
    monkeypatch.setattr(transport, "inventory", inventory)
    monkeypatch.setattr(transport, "call", call)
    with TestClient(app, base_url=BASE) as client:
        yield Pair(
            client,
            harness.control,
            harness.executor,
            destination,
            harness.executor_id,
            dst_id,
            online,
            overlays,
            called,
        )


def api_pair(pair: Pair) -> dict[str, str]:
    return {
        "source_executor_id": pair.src_id,
        "destination_executor_id": pair.dst_id,
    }


def test_real_loopback_two_executors_one_shot_proof_and_cached_read(
    two_executors,
):
    pair = two_executors
    endpoint = "/api/ui/executors/peer-link"
    params = api_pair(pair)
    initial = pair.client.get(endpoint, params=params)
    assert initial.status_code == 200
    assert initial.json()["data"]["status"] == "untested"
    assert pair.called == []

    measured = pair.client.post(endpoint, json=params)
    assert measured.status_code == 200, measured.text
    result = measured.json()["data"]
    assert result["status"] == "peer_succeeded"
    assert result["evidence"] == "peer_handshake"
    assert result["tested_at"] is not None
    assert result["directional"] is True
    assert "token" not in str(result)
    assert "host" not in result
    assert pair.called == [
        (pair.dst_id, "peer.probe_prepare"),
        (pair.src_id, "peer.probe_connect"),
        (pair.dst_id, "peer.probe_cancel"),
    ]
    again = pair.client.get(endpoint, params=params)
    assert again.json()["data"] == result
    assert len(pair.called) == 3
    assert pair.destination.peer_probe._probes == {}
    # GET or POST must never create Session/Task/write-transfer resources.
    assert not pair.source.sessions.inventory()
    assert not pair.destination.sessions.inventory()


def test_overlay_mismatch_is_ineligible_not_success(two_executors):
    pair = two_executors
    pair.overlays[pair.dst_id] = TrustedOverlay(
        provider="tailscale",
        tailnet_id="different-tailnet",
        ipv4="100.100.100.11",
    )
    response = pair.client.post(
        "/api/ui/executors/peer-link", json=api_pair(pair)
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "ineligible"
    assert data["reason"] == "tailnet_mismatch"
    assert data["evidence"] == "overlay_only"
    assert data["fallback"] == "control_relay_not_tested"
    assert pair.called == []


def test_overlay_missing_is_ineligible(two_executors):
    pair = two_executors
    pair.overlays[pair.src_id] = None
    response = pair.client.post(
        "/api/ui/executors/peer-link", json=api_pair(pair)
    )
    assert response.json()["data"]["reason"] == "overlay_unavailable"
    assert pair.called == []


def test_offline_executor_is_unreachable_without_probe(two_executors):
    pair = two_executors
    pair.online[pair.src_id] = False
    response = pair.client.post(
        "/api/ui/executors/peer-link", json=api_pair(pair)
    )
    assert response.json()["data"]["status"] == "unreachable"
    assert response.json()["data"]["reason"] == "executor_offline"
    assert pair.called == []


def test_overlay_detected_but_unbindable_is_not_peer_succeeded(
    two_executors, monkeypatch
):
    pair = two_executors

    async def fail_bind(*args, **kwargs):
        raise OSError("cannot bind overlay")

    monkeypatch.setattr(peer_probe.asyncio, "start_server", fail_bind)
    response = pair.client.post(
        "/api/ui/executors/peer-link", json=api_pair(pair)
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unreachable"
    assert response.json()["data"]["reason"] == "receiver_unavailable"
    assert pair.called == [(pair.dst_id, "peer.probe_prepare")]


def test_probe_rate_limit_and_no_fake_retest(two_executors):
    pair = two_executors
    url = "/api/ui/executors/peer-link"
    assert pair.client.post(url, json=api_pair(pair)).status_code == 200
    n = len(pair.called)
    rejected = pair.client.post(url, json=api_pair(pair))
    assert rejected.status_code == 429
    assert rejected.json()["error"] == "LinkProbeBusy"
    assert len(pair.called) == n


def test_unknown_or_same_executor_rejected(two_executors):
    pair = two_executors
    url = "/api/ui/executors/peer-link"
    assert (
        pair.client.get(
            url,
            params={
                "source_executor_id": pair.src_id,
                "destination_executor_id": "not-trusted",
            },
        ).status_code
        == 404
    )
    assert (
        pair.client.post(
            url,
            json={
                "source_executor_id": pair.src_id,
                "destination_executor_id": pair.src_id,
            },
        ).status_code
        == 400
    )


def test_failed_real_connect_is_unreachable_and_listener_closes(
    two_executors, monkeypatch
):
    pair = two_executors

    async def fail_connect(host, port, **kwargs):
        raise OSError("blocked outbound peer TCP")

    monkeypatch.setattr(peer_probe.asyncio, "open_connection", fail_connect)
    response = pair.client.post(
        "/api/ui/executors/peer-link", json=api_pair(pair)
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unreachable"
    assert response.json()["data"]["reason"] == "peer_handshake_failed"
    assert pair.destination.peer_probe._probes == {}
    assert pair.called == [
        (pair.dst_id, "peer.probe_prepare"),
        (pair.src_id, "peer.probe_connect"),
        (pair.dst_id, "peer.probe_cancel"),
    ]


def test_rpc_timeout_is_explicit_and_never_success(two_executors, monkeypatch):
    pair = two_executors

    async def timed_out(*_args, **_kwargs):
        raise TimeoutError("RPC deadline")

    monkeypatch.setattr(pair.control.executor_transport, "call", timed_out)
    response = pair.client.post(
        "/api/ui/executors/peer-link", json=api_pair(pair)
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unreachable"
    assert response.json()["data"]["reason"] == "probe_timeout"
    assert pair.destination.peer_probe._probes == {}


def test_cached_result_becomes_stale_without_network_retest(
    two_executors, monkeypatch
):
    from workgate.control import peer_links

    pair = two_executors
    endpoint = "/api/ui/executors/peer-link"
    measured = pair.client.post(endpoint, json=api_pair(pair))
    assert measured.json()["data"]["status"] == "peer_succeeded"
    calls = len(pair.called)
    at = pair.control.peer_links._records[(pair.src_id, pair.dst_id)][0]
    monkeypatch.setattr(
        peer_links, "time", SimpleNamespace(monotonic=lambda: at + 400)
    )
    stale = pair.client.get(endpoint, params=api_pair(pair))
    assert stale.status_code == 200
    row = stale.json()["data"]
    assert row["status"] == "stale"
    assert row["previous_status"] == "peer_succeeded"
    assert row["tested_at"] == measured.json()["data"]["tested_at"]
    assert row["reason"] == "measurement_expired"
    assert len(pair.called) == calls


def test_probe_denied_without_write_scope(two_executors, monkeypatch):
    from workgate.control.http import executor_admin
    from workgate.oauth.core.context import MissingOAuthScopeError
    from workgate.oauth.core.scopes import SCOPE_SHELL_WRITE

    pair = two_executors

    def require(scopes):
        if SCOPE_SHELL_WRITE in scopes:
            raise MissingOAuthScopeError("shell:write")

    monkeypatch.setattr(executor_admin, "require_oauth_scopes", require)
    url = "/api/ui/executors/peer-link"
    assert pair.client.get(url, params=api_pair(pair)).status_code == 200
    assert pair.client.post(url, json=api_pair(pair)).status_code == 403
    assert pair.called == []


def test_process_wide_probe_busy_rejects_without_network(two_executors):
    pair = two_executors
    service = pair.control.peer_links
    service._active.update({("a", "b"), ("c", "d")})
    response = pair.client.post(
        "/api/ui/executors/peer-link", json=api_pair(pair)
    )
    assert response.status_code == 429
    assert pair.called == []
    service._active.clear()


def test_last_success_becomes_stale_when_executor_offline(two_executors):
    pair = two_executors
    endpoint = "/api/ui/executors/peer-link"
    measured = pair.client.post(endpoint, json=api_pair(pair))
    assert measured.json()["data"]["status"] == "peer_succeeded"
    original_calls = len(pair.called)
    pair.online[pair.dst_id] = False
    state = pair.client.get(endpoint, params=api_pair(pair))
    assert state.status_code == 200
    row = state.json()["data"]
    assert row["status"] == "stale"
    assert row["reason"] == "executor_offline"
    assert row["previous_status"] == "peer_succeeded"
    assert len(pair.called) == original_calls


def test_probe_timeout_clears_active_state(two_executors, monkeypatch):
    from workgate.control import peer_links

    pair = two_executors

    async def never_finishes(_src, _dst):
        await asyncio.sleep(60)
        return "peer_succeeded", None

    monkeypatch.setattr(peer_links, "_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pair.control.peer_links, "_probe_real", never_finishes)
    response = pair.client.post(
        "/api/ui/executors/peer-link", json=api_pair(pair)
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unreachable"
    assert response.json()["data"]["reason"] == "probe_timeout"
    assert pair.control.peer_links._active == set()
    assert pair.called == []


def test_directional_probe_does_not_infer_reverse_link(two_executors):
    pair = two_executors
    url = "/api/ui/executors/peer-link"
    assert (
        pair.client.post(url, json=api_pair(pair)).json()["data"]["status"]
        == "peer_succeeded"
    )
    backwards = pair.client.get(
        url,
        params={
            "source_executor_id": pair.dst_id,
            "destination_executor_id": pair.src_id,
        },
    )
    assert backwards.status_code == 200
    assert backwards.json()["data"]["status"] == "untested"
