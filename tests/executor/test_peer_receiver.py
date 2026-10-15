"""Private peer receiver: one-use auth, bounded staging, cancellation and cleanup."""

import asyncio
import contextlib
import hashlib
from pathlib import Path
from typing import TypedDict

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


class _PeerArgs(TypedDict):
    session_id: str
    path: str
    transfer_id: str
    expected_bytes: int
    expected_sha256: str
    overwrite: bool
    tailnet_id: str


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


async def _assert_stopped(info: dict[str, str | int]) -> None:
    # TCP backlog can still complete a handshake after close; it must never
    # consume or accept further transfer bytes.
    try:
        reader, writer = await asyncio.open_connection(
            str(info["host"]), int(info["port"])
        )
    except OSError:
        return
    try:
        writer.write(str(info["token"]).encode() + b"\nabcdefgh")
        with pytest.raises((ConnectionError, TimeoutError, OSError)):
            await asyncio.wait_for(reader.readexactly(3), 0.3)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()


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
        await _assert_stopped(info)
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
        await _assert_stopped(info)
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


@pytest.mark.asyncio
async def test_cancel_does_not_release_transfer_identity_before_disk_cleanup(
    tmp_path, monkeypatch
):
    manager, _, sid, _ = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "e" * 22
    args: _PeerArgs = {
        "session_id": sid,
        "path": "staged.bin",
        "transfer_id": tid,
        "expected_bytes": 2,
        "expected_sha256": hashlib.sha256(b"ab").hexdigest(),
        "overwrite": True,
        "tailnet_id": TAILNET,
    }
    await manager.prepare(**args)
    entered, release = asyncio.Event(), asyncio.Event()
    abort = manager._abort

    async def delayed_abort(*args):
        entered.set()
        await release.wait()
        await abort(*args)

    monkeypatch.setattr(manager, "_abort", delayed_abort)
    cancelling = asyncio.create_task(manager.cancel(tid))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        preparing = asyncio.create_task(manager.prepare(**args))
        await asyncio.sleep(0.05)
        assert not preparing.done()
        release.set()
        assert await cancelling is True
        info = await preparing
        assert await _connect(info, str(info["token"]), b"ab") == b"OK\n"
        await _wait_status(manager, tid, "received")
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancel_cleanup_failure_remains_retryable(tmp_path, monkeypatch):
    manager, context, sid, _ = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "f" * 22
    await manager.prepare(
        session_id=sid,
        path="staged.bin",
        transfer_id=tid,
        expected_bytes=2,
        expected_sha256=hashlib.sha256(b"ab").hexdigest(),
        overwrite=True,
        tailnet_id=TAILNET,
    )
    abort = manager._abort
    attempts = 0

    async def fail_once(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected failure")
        await abort(*args)

    monkeypatch.setattr(manager, "_abort", fail_once)
    try:
        with pytest.raises(OSError, match="injected failure"):
            await manager.cancel(tid)
        assert manager.status(tid) == "cleanup_failed"
        assert await manager.cancel(tid) is True
        assert manager.status(tid) == "unknown"
        begin = transfer_begin_write(
            "staged.bin", True, 2, tid, session_id=sid, context=context
        )
        assert begin.offset == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_waiting_lease_expires_but_active_transfer_survives_total_timeout(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(peer_module, "_CONNECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(peer_module, "_IDLE_TIMEOUT_S", 0.5)
    manager, _, sid, _ = _receiver(tmp_path, monkeypatch)
    pending = "copy_" + "g" * 22
    try:
        await manager.prepare(
            session_id=sid,
            path="pending.bin",
            transfer_id=pending,
            expected_bytes=1,
            expected_sha256=hashlib.sha256(b"a").hexdigest(),
            overwrite=True,
            tailnet_id=TAILNET,
        )
        await _wait_status(manager, pending, "unknown")

        tid = "copy_" + "h" * 22
        info = await manager.prepare(
            session_id=sid,
            path="slow.bin",
            transfer_id=tid,
            expected_bytes=2,
            expected_sha256=hashlib.sha256(b"ab").hexdigest(),
            overwrite=True,
            tailnet_id=TAILNET,
        )
        reader, writer = await asyncio.open_connection(
            str(info["host"]), int(info["port"])
        )
        try:
            writer.write(str(info["token"]).encode() + b"\na")
            await writer.drain()
            await _wait_status(manager, tid, "receiving")
            await asyncio.sleep(0.08)
            assert manager.status(tid) == "receiving"
            writer.write(b"b")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 2) == b"OK\n"
            assert manager.status(tid) == "received"
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_prepare_bind_error_retains_cleanup_failure_for_retry(
    tmp_path, monkeypatch
):
    manager, _, sid, _ = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "m" * 22
    bind_failed = OSError("injected bind error")

    async def fail_bind(*args, **kwargs):
        raise bind_failed

    abort = manager._abort
    attempts = 0

    async def fail_first_cleanup(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected cleanup error")
        await abort(*args)

    monkeypatch.setattr(peer_module.asyncio, "start_server", fail_bind)
    monkeypatch.setattr(manager, "_abort", fail_first_cleanup)
    try:
        with pytest.raises(OSError, match="cleanup error"):
            await manager.prepare(
                session_id=sid,
                path="staged.bin",
                transfer_id=tid,
                expected_bytes=2,
                expected_sha256=hashlib.sha256(b"ab").hexdigest(),
                overwrite=True,
                tailnet_id=TAILNET,
            )
        assert manager.status(tid) == "cleanup_failed"
        assert await manager.cancel(tid) is True
        assert manager.status(tid) == "unknown"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancel_waits_for_inflight_disk_write_before_abort(
    tmp_path, monkeypatch
):
    import threading

    manager, _, sid, workspace = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "n" * 22
    block = b"abcd"
    info = await manager.prepare(
        session_id=sid,
        path="out.bin",
        transfer_id=tid,
        expected_bytes=len(block),
        expected_sha256=hashlib.sha256(block).hexdigest(),
        overwrite=True,
        tailnet_id=TAILNET,
    )
    original = peer_module.transfer_write_bytes
    entered, release = threading.Event(), threading.Event()

    def slow_write(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(peer_module, "transfer_write_bytes", slow_write)
    reader, writer = await asyncio.open_connection(
        str(info["host"]), int(info["port"])
    )
    try:
        writer.write(str(info["token"]).encode() + b"\n" + block)
        await writer.drain()
        assert await asyncio.to_thread(entered.wait, 2)
        cancelling = asyncio.create_task(manager.cancel(tid))
        await asyncio.sleep(0.05)
        assert not cancelling.done()
        release.set()
        assert await asyncio.wait_for(cancelling, 3) is True
        assert manager.status(tid) == "unknown"
        assert not (workspace / "out.bin").exists()
    finally:
        release.set()
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancel_revokes_pending_authenticated_claims(
    tmp_path, monkeypatch
):
    manager, _, sid, workspace = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "p" * 22
    info = await manager.prepare(
        session_id=sid,
        path="late.bin",
        transfer_id=tid,
        expected_bytes=2,
        expected_sha256=hashlib.sha256(b"ab").hexdigest(),
        overwrite=True,
        tailnet_id=TAILNET,
    )
    reader, writer = await asyncio.open_connection(
        str(info["host"]), int(info["port"])
    )
    try:
        await asyncio.sleep(0.02)
        assert await manager.cancel(tid)
        writer.write(str(info["token"]).encode() + b"\nab")
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.drain()
        with contextlib.suppress(ConnectionResetError):
            assert await asyncio.wait_for(reader.read(16), 1) == b""
        assert not (workspace / "late.bin").exists()
        assert manager.status(tid) == "unknown"
    finally:
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancel_during_begin_waits_for_thread_and_cleans_receipt(
    tmp_path, monkeypatch
):
    import threading

    manager, context, sid, _ = _receiver(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = peer_module.transfer_begin_write

    def slow_begin(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(peer_module, "transfer_begin_write", slow_begin)
    tid = "copy_" + "q" * 22
    task = asyncio.create_task(
        manager.prepare(
            session_id=sid,
            path="cancelled.bin",
            transfer_id=tid,
            expected_bytes=3,
            expected_sha256=hashlib.sha256(b"abc").hexdigest(),
            overwrite=True,
            tailnet_id=TAILNET,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.status(tid) == "unknown"
        begun = transfer_begin_write(
            "cancelled.bin", True, 3, tid, session_id=sid, context=context
        )
        assert begun.resumed is False
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["runtime", "session"])
async def test_cleanup_failure_does_not_leave_other_listeners_open(
    tmp_path, monkeypatch, scope
):
    manager, _, sid, _ = _receiver(tmp_path, monkeypatch)
    tids = ("copy_" + "u" * 22, "copy_" + "v" * 22)
    endpoints = []
    for tid in tids:
        endpoints.append(
            await manager.prepare(
                session_id=sid,
                path=f"{tid}.bin",
                transfer_id=tid,
                expected_bytes=1,
                expected_sha256=hashlib.sha256(b"x").hexdigest(),
                overwrite=True,
                tailnet_id=TAILNET,
            )
        )
    abort = manager._abort
    failed = True

    async def fail_once(session_id, path, transfer_id):
        nonlocal failed
        if transfer_id == tids[0] and failed:
            failed = False
            raise OSError("injected disk failure")
        return await abort(session_id, path, transfer_id)

    monkeypatch.setattr(manager, "_abort", fail_once)
    try:
        with pytest.raises(
            ExceptionGroup, match="peer receiver cleanup failed"
        ) as exc:
            if scope == "runtime":
                await manager.aclose()
            else:
                await manager.cancel_session(sid)
        assert len(exc.value.exceptions) == 1
        assert isinstance(exc.value.exceptions[0], OSError)
        assert manager.status(tids[0]) == "cleanup_failed"
        assert manager.status(tids[1]) == "unknown"
        for info in endpoints:
            await _assert_stopped(info)
        if scope == "runtime":
            await manager.aclose()
        else:
            await manager.cancel_session(sid)
        assert manager.status(tids[0]) == "unknown"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_begin_partial_failure_is_rolled_back(tmp_path, monkeypatch):
    manager, context, sid, _ = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "w" * 22
    original = peer_module.transfer_begin_write

    def fail_after_allocating(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected write receipt I/O failure")

    monkeypatch.setattr(
        peer_module, "transfer_begin_write", fail_after_allocating
    )
    try:
        with pytest.raises(OSError, match="receipt I/O failure"):
            await manager.prepare(
                session_id=sid,
                path="partial.bin",
                transfer_id=tid,
                expected_bytes=3,
                expected_sha256=hashlib.sha256(b"abc").hexdigest(),
                overwrite=True,
                tailnet_id=TAILNET,
            )
        assert manager.status(tid) == "unknown"
        begun = transfer_begin_write(
            "partial.bin", True, 3, tid, session_id=sid, context=context
        )
        assert begun.resumed is False
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_begin_does_not_steal_existing_transfer_receipt(
    tmp_path, monkeypatch
):
    manager, context, sid, _ = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "x" * 22
    original = transfer_begin_write(
        "owned.bin", True, 3, tid, session_id=sid, context=context
    )
    assert not original.resumed
    try:
        with pytest.raises(ValueError, match="new transfer identity"):
            await manager.prepare(
                session_id=sid,
                path="owned.bin",
                transfer_id=tid,
                expected_bytes=3,
                expected_sha256=hashlib.sha256(b"abc").hexdigest(),
                overwrite=True,
                tailnet_id=TAILNET,
            )
        still_owned = transfer_begin_write(
            "owned.bin", True, 3, tid, session_id=sid, context=context
        )
        assert still_owned.resumed is True
        assert manager.status(tid) == "unknown"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_begin_failure_cleanup_can_be_retried(tmp_path, monkeypatch):
    manager, context, sid, _ = _receiver(tmp_path, monkeypatch)
    tid = "copy_" + "y" * 22
    original = peer_module.transfer_begin_write
    abort = manager._abort
    fail_abort = True

    def fail_after_allocating(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected begin failure")

    async def fail_first_abort(*args):
        nonlocal fail_abort
        if fail_abort:
            fail_abort = False
            raise OSError("injected cleanup failure")
        await abort(*args)

    monkeypatch.setattr(
        peer_module, "transfer_begin_write", fail_after_allocating
    )
    monkeypatch.setattr(manager, "_abort", fail_first_abort)
    try:
        with pytest.raises(OSError, match="injected begin failure"):
            await manager.prepare(
                session_id=sid,
                path="partial.bin",
                transfer_id=tid,
                expected_bytes=3,
                expected_sha256=hashlib.sha256(b"abc").hexdigest(),
                overwrite=True,
                tailnet_id=TAILNET,
            )
        assert manager.status(tid) == "cleanup_failed"
        assert await manager.cancel(tid)
        assert manager.status(tid) == "unknown"
        fresh = transfer_begin_write(
            "partial.bin", True, 3, tid, session_id=sid, context=context
        )
        assert fresh.resumed is False
    finally:
        await manager.aclose()
