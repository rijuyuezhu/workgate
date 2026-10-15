"""Private peer receiver: one-use auth, bounded staging, cancellation and cleanup."""

import asyncio
import contextlib
import hashlib
from pathlib import Path

import pytest

import workgate.executor.peer_receiver as peer_module
from tests.support.sessions import build_tool_session_store
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.peer_receiver import PeerReceiverManager
from workgate.executor.transfer import (
    TransferContext,
    transfer_begin_write,
    transfer_finish_write,
)
from workgate.protocol.executor import TrustedOverlay
from workgate.protocol.ids import new_session_id

TAILNET = "tailnet-test"


def _receiver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        agent_bridge_enabled=False,
    )
    config = resolve_executor_config(settings)
    store = build_tool_session_store(settings)
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workspace)
    monkeypatch.setattr(
        peer_module,
        "detect_tailscale_overlay",
        lambda: TrustedOverlay.model_construct(
            provider="tailscale", tailnet_id=TAILNET, ipv4="127.0.0.1"
        ),
    )
    return (
        PeerReceiverManager(config, store),
        TransferContext(config, store),
        session_id,
        workspace,
    )


async def _connect(
    info: dict[str, str | int], token: str, data: bytes
) -> bytes:
    reader, writer = await asyncio.open_connection(
        str(info["host"]), int(info["port"])
    )
    try:
        writer.write(token.encode() + b"\n" + data)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 2)
    finally:
        writer.close()
        await writer.wait_closed()


async def _wait_status(
    manager: PeerReceiverManager, transfer_id: str, want: str
):
    for _ in range(100):
        if manager.status(transfer_id) == want:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {want}, got {manager.status(transfer_id)}")


@pytest.mark.asyncio
async def test_peer_receiver_one_use_transactional_success(
    tmp_path, monkeypatch
):
    manager, context, sid, workspace = _receiver(tmp_path, monkeypatch)
    data = b"source bytes" * 10000
    digest = hashlib.sha256(data).hexdigest()
    tid = "copy_" + "a" * 22
    try:
        info = await manager.prepare(
            session_id=sid,
            path="output.bin",
            transfer_id=tid,
            expected_bytes=len(data),
            expected_sha256=digest,
            overwrite=True,
            tailnet_id=TAILNET,
        )
        assert not (workspace / "output.bin").exists()
        assert await _connect(info, "wrong-token", b"") == b""
        assert manager.status(tid) == "pending"
        assert await _connect(info, str(info["token"]), data) == b"OK\n"
        await _wait_status(manager, tid, "received")
        assert not (workspace / "output.bin").exists()
        finished = transfer_finish_write(
            "output.bin",
            tid,
            len(data),
            digest,
            session_id=sid,
            context=context,
        )
        assert finished.completed
        assert (workspace / "output.bin").read_bytes() == data
        # Closing the receiver must not erase already published output.
        await manager.cancel(tid)
        assert (workspace / "output.bin").read_bytes() == data
        # TCP backlog can still complete a handshake after close; it must never
        # consume or accept any further transfer data.
        try:
            reader, writer = await asyncio.open_connection(
                str(info["host"]), int(info["port"])
            )
        except OSError:
            pass
        else:
            writer.write(str(info["token"]).encode() + b"\nabcdefgh")
            with pytest.raises((ConnectionError, TimeoutError, OSError)):
                await asyncio.wait_for(reader.readexactly(3), 0.3)
            writer.close()
            with contextlib.suppress(ConnectionResetError):
                await writer.wait_closed()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_peer_receiver_digest_mismatch_preserves_destination(
    tmp_path, monkeypatch
):
    manager, context, sid, workspace = _receiver(tmp_path, monkeypatch)
    (workspace / "output.bin").write_bytes(b"original")
    tid = "copy_" + "b" * 22
    info = await manager.prepare(
        session_id=sid,
        path="output.bin",
        transfer_id=tid,
        expected_bytes=5,
        expected_sha256=hashlib.sha256(b"hello").hexdigest(),
        overwrite=True,
        tailnet_id=TAILNET,
    )
    try:
        assert await _connect(info, str(info["token"]), b"world") == b""
        await _wait_status(manager, tid, "failed")
        assert (workspace / "output.bin").read_bytes() == b"original"
        begin = transfer_begin_write(
            "output.bin", True, 5, tid, session_id=sid, context=context
        )
        assert begin.offset == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_peer_receiver_cancellation_and_expiry(tmp_path, monkeypatch):
    manager, context, sid, workspace = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "c" * 22
    info = await manager.prepare(
        session_id=sid,
        path="staged.bin",
        transfer_id=tid,
        expected_bytes=8,
        expected_sha256=hashlib.sha256(b"abcdefgh").hexdigest(),
        overwrite=True,
        tailnet_id=TAILNET,
    )
    try:
        await manager.cancel_session(sid)
        assert manager.status(tid) == "unknown"
        assert not (workspace / "staged.bin").exists()
        # TCP backlog can still complete a handshake after close; it must never
        # consume or accept any further transfer data.
        try:
            reader, writer = await asyncio.open_connection(
                str(info["host"]), int(info["port"])
            )
        except OSError:
            pass
        else:
            writer.write(str(info["token"]).encode() + b"\nabcdefgh")
            with pytest.raises((ConnectionError, TimeoutError, OSError)):
                await asyncio.wait_for(reader.readexactly(3), 0.3)
            writer.close()
            with contextlib.suppress(ConnectionResetError):
                await writer.wait_closed()
        begin = transfer_begin_write(
            "staged.bin", True, 8, tid, session_id=sid, context=context
        )
        assert begin.offset == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_peer_receiver_rejects_changed_overlay_and_over_capacity(
    tmp_path, monkeypatch
):
    manager, _, sid, _ = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "d" * 22
    try:
        with pytest.raises(RuntimeError, match="overlay"):
            await manager.prepare(
                session_id=sid,
                path="staged.bin",
                transfer_id=tid,
                expected_bytes=0,
                expected_sha256=hashlib.sha256(b"").hexdigest(),
                overwrite=True,
                tailnet_id="another-tailnet",
            )
        assert manager.status(tid) == "unknown"
        with pytest.raises(ValueError, match="digest"):
            await manager.prepare(
                session_id=sid,
                path="staged.bin",
                transfer_id=tid,
                expected_bytes=0,
                expected_sha256="bad",
                overwrite=True,
                tailnet_id=TAILNET,
            )
        with pytest.raises(ValueError, match="size"):
            await manager.prepare(
                session_id=sid,
                path="staged.bin",
                transfer_id=tid,
                expected_bytes=-1,
                expected_sha256=hashlib.sha256(b"").hexdigest(),
                overwrite=True,
                tailnet_id=TAILNET,
            )
    finally:
        await manager.aclose()
