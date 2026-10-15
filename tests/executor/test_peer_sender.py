"""Source-to-receiver native byte streaming across two separate runtimes."""

import hashlib

import pytest

import workgate.executor.peer_receiver as receiver_module
import workgate.executor.peer_sender as sender_module
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.peer_sender import _validate_endpoint
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.transfer import TransferContext, transfer_finish_write
from workgate.protocol.executor import ExecutorCommand, TrustedOverlay
from workgate.protocol.ids import new_command_id, new_session_id


@pytest.fixture
def peer_runtimes(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    src = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=source_dir,
                state_dir=tmp_path / "source_state",
                agent_bridge_enabled=False,
            )
        ),
        enable_control_connection=False,
    )
    dst = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=dest_dir,
                state_dir=tmp_path / "dest_state",
                agent_bridge_enabled=False,
            )
        ),
        enable_control_connection=False,
    )
    src_sid, dst_sid = new_session_id(), new_session_id()
    src.services.tool_session_store.create_session(
        session_id=str(src_sid), workdir=source_dir
    )
    dst.services.tool_session_store.create_session(
        session_id=str(dst_sid), workdir=dest_dir
    )
    overlay = TrustedOverlay.model_construct(
        provider="tailscale", tailnet_id="test-tailnet", ipv4="127.0.0.1"
    )
    monkeypatch.setattr(
        receiver_module, "detect_tailscale_overlay", lambda: overlay
    )
    monkeypatch.setattr(
        sender_module, "detect_tailscale_overlay", lambda: overlay
    )
    # Localhost is used only in isolated tests, never allowed by production sender.
    monkeypatch.setattr(sender_module, "_validate_endpoint", lambda *args: None)
    return src, dst, src_sid, dst_sid, source_dir, dest_dir


async def _send(src, sid, info, size, digest, **overrides):
    args = {
        "path": "source.bin",
        "transfer_id": str(info["transfer_id"]),
        "host": str(info["host"]),
        "port": int(info["port"]),
        "token": str(info["token"]),
        "tailnet_id": "test-tailnet",
        "expected_bytes": size,
        "expected_sha256": digest,
    }
    args.update(overrides)
    return await src._execute_protocol_command(
        ExecutorCommand(
            id=new_command_id(),
            op="transfer.peer_send",
            session_id=sid,
            args=args,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"", b"hello peer", b"X" * 400_000])
async def test_two_runtime_sender_receiver_then_atomic_commit(
    peer_runtimes, data
):
    src, dst, src_sid, dst_sid, source_dir, dest_dir = peer_runtimes
    tid = "copy_" + "a" * 22
    (source_dir / "source.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    try:
        info = await dst.peer_receiver.prepare(
            session_id=str(dst_sid),
            path="output.bin",
            transfer_id=tid,
            expected_bytes=len(data),
            expected_sha256=digest,
            overwrite=True,
            tailnet_id="test-tailnet",
        )
        result = await _send(src, src_sid, info, len(data), digest)
        assert result == {
            "transfer_id": tid,
            "bytes": len(data),
            "sha256": digest,
        }
        assert dst.peer_receiver.status(tid) == "received"
        assert not (dest_dir / "output.bin").exists()
        receipt = transfer_finish_write(
            "output.bin",
            tid,
            len(data),
            digest,
            session_id=str(dst_sid),
            context=TransferContext(
                dst.config, dst.services.tool_session_store
            ),
        )
        assert receipt.completed
        assert (dest_dir / "output.bin").read_bytes() == data
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.asyncio
async def test_wrong_token_never_sends_receipt(peer_runtimes):
    src, dst, src_sid, dst_sid, source_dir, dest_dir = peer_runtimes
    data = b"secret content"
    (source_dir / "source.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    try:
        info = await dst.peer_receiver.prepare(
            session_id=str(dst_sid),
            path="output.bin",
            transfer_id="copy_" + "b" * 22,
            expected_bytes=len(data),
            expected_sha256=digest,
            overwrite=True,
            tailnet_id="test-tailnet",
        )
        with pytest.raises((RuntimeError, ConnectionError, OSError)):
            await _send(src, src_sid, info, len(data), digest, token="A" * 43)
        assert dst.peer_receiver.status(str(info["transfer_id"])) == "pending"
        assert not (dest_dir / "output.bin").exists()
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.asyncio
async def test_sender_rejects_changed_overlay_before_connect(
    peer_runtimes, monkeypatch
):
    src, dst, src_sid, dst_sid, source_dir, _ = peer_runtimes
    data = b"hello"
    (source_dir / "source.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    try:
        info = await dst.peer_receiver.prepare(
            session_id=str(dst_sid),
            path="out.bin",
            transfer_id="copy_" + "c" * 22,
            expected_bytes=len(data),
            expected_sha256=digest,
            overwrite=True,
            tailnet_id="test-tailnet",
        )
        monkeypatch.setattr(
            sender_module, "detect_tailscale_overlay", lambda: None
        )
        with pytest.raises(RuntimeError, match="overlay"):
            await _send(src, src_sid, info, len(data), digest)
        assert dst.peer_receiver.status(str(info["transfer_id"])) == "pending"
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.asyncio
async def test_sender_does_not_ack_changed_source(peer_runtimes):
    src, dst, src_sid, dst_sid, source_dir, dest_dir = peer_runtimes
    data = b"frozen"
    (source_dir / "source.bin").write_bytes(b"broken")
    try:
        info = await dst.peer_receiver.prepare(
            session_id=str(dst_sid),
            path="out.bin",
            transfer_id="copy_" + "d" * 22,
            expected_bytes=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
            overwrite=True,
            tailnet_id="test-tailnet",
        )
        with pytest.raises((ValueError, RuntimeError)):
            await _send(
                src, src_sid, info, len(data), hashlib.sha256(data).hexdigest()
            )
        assert not (dest_dir / "out.bin").exists()
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "0.0.0.0", "192.168.1.1", "8.8.8.8", "::1", "invalid"]
)
def test_sender_rejects_non_tailnet_endpoint(host):
    with pytest.raises(ValueError, match="Tailnet IPv4"):
        _validate_endpoint(host, 10000, "A" * 43)


@pytest.mark.parametrize("port", [-1, 0, 65536])
def test_sender_rejects_invalid_port(port):
    with pytest.raises(ValueError, match="port"):
        _validate_endpoint("100.80.1.2", port, "A" * 43)


def test_sender_accepts_tailnet_address_and_rejects_invalid_token():
    _validate_endpoint("100.80.1.2", 18765, "A" * 43)
    for token in ("tiny", "A" * 43 + "\n", "A" * 129):
        with pytest.raises(ValueError, match="token"):
            _validate_endpoint("100.80.1.2", 18765, token)


@pytest.mark.asyncio
async def test_sender_rejects_unreachable_peer_without_exposing_token(
    peer_runtimes,
):
    src, dst, src_sid, _dst_sid, source_dir, _ = peer_runtimes
    (source_dir / "source.bin").write_bytes(b"hello")
    info = {
        "host": "127.0.0.1",
        "port": 1,
        "token": "A" * 43,
        "transfer_id": "copy_" + "e" * 22,
    }
    try:
        with pytest.raises(
            RuntimeError, match="trusted peer connection"
        ) as exc:
            await _send(
                src, src_sid, info, 5, hashlib.sha256(b"hello").hexdigest()
            )
        assert info["token"] not in str(exc.value)
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.asyncio
async def test_sender_refuses_stale_session(peer_runtimes):
    src, dst, src_sid, _dst_sid, source_dir, _ = peer_runtimes
    (source_dir / "source.bin").write_bytes(b"hello")
    await src.sessions.terminate(str(src_sid))
    try:
        info = {
            "host": "127.0.0.1",
            "port": 1,
            "token": "A" * 43,
            "transfer_id": "copy_" + "f" * 22,
        }
        with pytest.raises((RuntimeError, ValueError)):
            await _send(
                src, src_sid, info, 5, hashlib.sha256(b"hello").hexdigest()
            )
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_key,bad_value",
    [
        ("port", "123"),
        ("port", True),
        ("expected_bytes", "5"),
        ("expected_bytes", True),
        ("token", 100),
        ("host", None),
        ("_workgate_unbound_temp", "false"),
        ("chunk_size", "1000"),
    ],
)
async def test_sender_requires_exact_rpc_argument_types(
    peer_runtimes, bad_key, bad_value
):
    src, dst, src_sid, _dst_sid, source_dir, _ = peer_runtimes
    (source_dir / "source.bin").write_bytes(b"hello")
    info = {
        "host": "127.0.0.1",
        "port": 1,
        "token": "A" * 43,
        "transfer_id": "copy_" + "g" * 22,
    }
    try:
        with pytest.raises(ValueError, match=f"peer {bad_key} must be"):
            await _send(
                src,
                src_sid,
                info,
                5,
                hashlib.sha256(b"hello").hexdigest(),
                **{bad_key: bad_value},
            )
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.asyncio
async def test_sender_streams_packed_directory_archive(peer_runtimes):
    from workgate.executor.transfer import transfer_pack_dir

    src, dst, src_sid, dst_sid, source_dir, dest_dir = peer_runtimes
    folder = source_dir / "folder"
    folder.mkdir()
    (folder / "entry.txt").write_text("archive data")
    packed = transfer_pack_dir(
        "folder",
        compression="gz",
        session_id=str(src_sid),
        context=TransferContext(src.config, src.services.tool_session_store),
    )
    tid = "copy_" + "h" * 22
    try:
        info = await dst.peer_receiver.prepare(
            session_id=str(dst_sid),
            path="archive.tar.gz",
            transfer_id=tid,
            expected_bytes=packed.bytes,
            expected_sha256=packed.sha256,
            overwrite=True,
            tailnet_id="test-tailnet",
        )
        result = await _send(
            src,
            src_sid,
            info,
            packed.bytes,
            packed.sha256,
            path=packed.archive_path,
            _workgate_unbound_temp=True,
        )
        assert result["bytes"] == packed.bytes
        await dst.peer_receiver.cancel(tid)
        assert not (dest_dir / "archive.tar.gz").exists()
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.asyncio
async def test_source_session_workdir_change_interrupts_stream(
    peer_runtimes, tmp_path, monkeypatch
):
    import asyncio

    src, dst, src_sid, dst_sid, source_dir, dest_dir = peer_runtimes
    data = b"abcdef"
    (source_dir / "source.bin").write_bytes(data)
    other = tmp_path / "new-workdir"
    other.mkdir()
    digest = hashlib.sha256(data).hexdigest()
    ready, release = asyncio.Event(), asyncio.Event()

    async def paused_stream(*args, **kwargs):
        yield data[:3]
        ready.set()
        await release.wait()
        yield data[3:]

    monkeypatch.setattr(sender_module, "_source_content", paused_stream)
    try:
        info = await dst.peer_receiver.prepare(
            session_id=str(dst_sid),
            path="out.bin",
            transfer_id="copy_" + "i" * 22,
            expected_bytes=len(data),
            expected_sha256=digest,
            overwrite=True,
            tailnet_id="test-tailnet",
        )
        sending = asyncio.create_task(
            _send(src, src_sid, info, len(data), digest)
        )
        try:
            await asyncio.wait_for(ready.wait(), 3)
            await src.sessions.change_workdir(str(src_sid), str(other))
            release.set()
            with pytest.raises(RuntimeError, match="workdir changed"):
                await asyncio.wait_for(sending, 3)
            assert not (dest_dir / "out.bin").exists()
        finally:
            release.set()
            await asyncio.gather(sending, return_exceptions=True)
    finally:
        await src.aclose()
        await dst.aclose()


@pytest.mark.asyncio
async def test_sender_does_not_hang_when_peer_stalls_socket_close(
    peer_runtimes, monkeypatch
):
    import asyncio

    src, dst, src_sid, _dst_sid, source_dir, _ = peer_runtimes
    data = b"S" * (4 * 1024 * 1024)
    (source_dir / "source.bin").write_bytes(data)
    monkeypatch.setattr(sender_module, "_IO_TIMEOUT_S", 0.1)
    monkeypatch.setattr(sender_module, "_CONNECT_TIMEOUT_S", 0.1)

    async def stalled_peer(reader, writer):
        try:
            await asyncio.sleep(2)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(stalled_peer, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        info = {
            "host": "127.0.0.1",
            "port": port,
            "token": "A" * 43,
            "transfer_id": "copy_" + "j" * 22,
        }
        with pytest.raises((TimeoutError, RuntimeError, ConnectionError)):
            await asyncio.wait_for(
                _send(
                    src,
                    src_sid,
                    info,
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                ),
                1,
            )
    finally:
        server.close()
        await server.wait_closed()
        await src.aclose()
        await dst.aclose()
