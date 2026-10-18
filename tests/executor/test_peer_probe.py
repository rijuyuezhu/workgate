"""Sessionless one-shot Peer probe: bounded listener, capability and expiration."""

import asyncio

import pytest

from workgate.executor import peer_probe
from workgate.protocol.executor import TrustedOverlay


@pytest.fixture
def tailnet_loopback(monkeypatch):
    overlay = TrustedOverlay(
        provider="tailscale", tailnet_id="test-tailnet", ipv4="100.100.100.11"
    )
    monkeypatch.setattr(peer_probe, "detect_tailscale_overlay", lambda: overlay)
    original_listen = asyncio.start_server
    original_connect = asyncio.open_connection

    async def bind(handler, host, port, **kwargs):
        assert host == "100.100.100.11"
        return await original_listen(handler, "127.0.0.1", port, **kwargs)

    async def connect(host, port, **kwargs):
        assert host == "100.100.100.11"
        return await original_connect("127.0.0.1", port, **kwargs)

    monkeypatch.setattr(peer_probe.asyncio, "start_server", bind)
    monkeypatch.setattr(peer_probe.asyncio, "open_connection", connect)
    return overlay


@pytest.mark.asyncio
async def test_one_shot_rejects_wrong_token_then_accepts_once(tailnet_loopback):
    _ = tailnet_loopback
    manager = peer_probe.PeerProbeManager()
    try:
        prepared = await manager.prepare("test-tailnet")
        assert len(prepared["token"]) == 43
        with pytest.raises(RuntimeError, match="handshake rejected"):
            await peer_probe.connect_peer_probe(
                host=prepared["host"],
                port=prepared["port"],
                token="z" * 43,
                tailnet_id="test-tailnet",
            )
        assert len(manager._probes) == 1
        assert await peer_probe.connect_peer_probe(
            host=prepared["host"],
            port=prepared["port"],
            token=prepared["token"],
            tailnet_id="test-tailnet",
        ) == {"connected": True}
        for _ in range(10):
            if not manager._probes:
                break
            await asyncio.sleep(0.01)
        assert not manager._probes
        with pytest.raises(RuntimeError, match="unreachable|rejected"):
            await peer_probe.connect_peer_probe(
                host=prepared["host"],
                port=prepared["port"],
                token=prepared["token"],
                tailnet_id="test-tailnet",
            )
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_probe_listener_capacity_and_cleanup(tailnet_loopback):
    _ = tailnet_loopback
    manager = peer_probe.PeerProbeManager()
    try:
        first = await manager.prepare("test-tailnet")
        second = await manager.prepare("test-tailnet")
        with pytest.raises(RuntimeError, match="capacity"):
            await manager.prepare("test-tailnet")
        await manager.cancel(first["probe_id"])
        await manager.cancel(second["probe_id"])
        await manager.cancel(first["probe_id"])
        assert not manager._probes
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unclaimed_listener_expires_without_control_cleanup(
    tailnet_loopback, monkeypatch
):
    _ = tailnet_loopback
    monkeypatch.setattr(peer_probe, "_LISTEN_SECONDS", 0.05)
    manager = peer_probe.PeerProbeManager()
    try:
        prepared = await manager.prepare("test-tailnet")
        await asyncio.sleep(0.12)
        assert not manager._probes
        with pytest.raises(RuntimeError, match="unreachable|rejected"):
            await peer_probe.connect_peer_probe(
                host=prepared["host"],
                port=prepared["port"],
                token=prepared["token"],
                tailnet_id="test-tailnet",
            )
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_probe_rejects_overlay_mismatch_and_out_of_tailnet_endpoint(
    tailnet_loopback,
):
    _ = tailnet_loopback
    manager = peer_probe.PeerProbeManager()
    try:
        with pytest.raises(RuntimeError, match="overlay"):
            await manager.prepare("different-tailnet")
        with pytest.raises(ValueError, match="Tailnet"):
            await peer_probe.connect_peer_probe(
                host="127.0.0.1",
                port=12345,
                token="a" * 43,
                tailnet_id="test-tailnet",
            )
        assert not manager._probes
    finally:
        await manager.aclose()
