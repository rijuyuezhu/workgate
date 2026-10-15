"""Real peer Executor receipts, duplicate copy ids, and lost commit acknowledgements."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from pydantic import BaseModel

import workgate.executor.peer_receiver as receiver_module
import workgate.executor.peer_sender as sender_module
from tests.workspace.test_session_copy import _fresh_cross_executor_service
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.control.session_copy import ControlSessionCopyService
from workgate.executor.runtime import build_executor_runtime
from workgate.persistence import use_state_store
from workgate.protocol.executor import (
    ExecutorCommand,
    ExecutorResult,
    OperationError,
    TrustedOverlay,
)
from workgate.protocol.ids import new_command_id


@asynccontextmanager
async def _real_peer_copy(
    tmp_path: Path, monkeypatch, kind: Literal["file", "dir"]
):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    if kind == "file":
        (source / "input").write_bytes(b"original")
    else:
        (source / "input").mkdir()
        (source / "input" / "entry.txt").write_text("original")
    service, src, dst, _ = _fresh_cross_executor_service(
        tmp_path / "control",
        source_kind=kind,
        payload=b"original",
        archive=b"placeholder",
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
    # Only local test TCP; production endpoint validation remains unchanged.
    monkeypatch.setattr(sender_module, "_validate_endpoint", lambda *args: None)
    runtimes = {}
    for record, workdir, label in (
        (src, source, "source"),
        (dst, destination, "destination"),
    ):
        runtime = build_executor_runtime(
            resolve_executor_config(
                Settings(
                    default_workdir=workdir,
                    state_dir=tmp_path / (label + "_state"),
                    agent_bridge_enabled=False,
                )
            ),
            enable_control_connection=False,
        )
        runtime.services.tool_session_store.create_session(
            session_id=str(record.session_id), workdir=workdir
        )
        runtimes[str(record.executor_id)] = runtime

    class Transport:
        def __init__(self):
            self.calls: list[str] = []
            self.drop_finish_ack = False
            self.drop_recovery_ack = False
            self.finish_was_lost = False
            self.archive_path: Path | None = None

        async def inventory(self, executor_id):
            _ = executor_id
            return SimpleNamespace(overlay=overlay)

        async def call(
            self, executor_id, op, args, *, session_id=None, timeout_s=None
        ):
            _ = timeout_s
            self.calls.append(op)
            command_id = new_command_id()
            runtime = runtimes[executor_id]
            try:
                with use_state_store(runtime.services.state_store):
                    result = await runtime._execute_protocol_command(
                        ExecutorCommand(
                            id=command_id,
                            op=op,
                            args=args,
                            session_id=session_id,
                        )
                    )
                if op == "transfer_alloc_temp_path":
                    self.archive_path = Path(result.path)
                if op == "transfer_finish_write" and self.drop_finish_ack:
                    self.drop_finish_ack = False
                    self.finish_was_lost = True
                    return ExecutorResult(
                        id=command_id,
                        ok=False,
                        error=OperationError(
                            code="operation_failed", message="finish ack lost"
                        ),
                    )
                if (
                    op == "transfer_begin_write"
                    and self.drop_recovery_ack
                    and self.finish_was_lost
                ):
                    self.drop_recovery_ack = False
                    return ExecutorResult(
                        id=command_id,
                        ok=False,
                        error=OperationError(
                            code="operation_failed", message="recovery offline"
                        ),
                    )
                if isinstance(result, BaseModel):
                    result = result.model_dump(mode="json")
                return ExecutorResult(id=command_id, ok=True, result=result)
            except Exception as exc:
                return ExecutorResult(
                    id=command_id,
                    ok=False,
                    error=OperationError(
                        code="operation_failed", message=type(exc).__name__
                    ),
                )

    transport = Transport()
    service._transport = transport  # type: ignore[assignment]
    args: dict[str, Any] = {
        "src_session_id": str(src.session_id),
        "src_path": "input",
        "dst_session_id": str(dst.session_id),
        "dst_path": "output",
        "kind": kind,
        "transfer_id": "copy_" + "a" * 22,
    }
    try:
        yield service, transport, args, destination
    finally:
        await service.aclose()
        for runtime in runtimes.values():
            await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "dir"])
async def test_peer_retry_preserves_user_edits_across_control_restart(
    tmp_path: Path, monkeypatch, kind: Literal["file", "dir"]
):
    async with _real_peer_copy(tmp_path, monkeypatch, kind) as (
        service,
        transport,
        args,
        destination,
    ):
        result = await asyncio.wait_for(service.copy(**args), 12)
        assert result.transport == "peer_direct"
        output = destination / "output"
        target = output if kind == "file" else output / "entry.txt"
        target.write_bytes(b"user edit")
        # New Control instance, same durable registry and unrelated user edit.
        another = ControlSessionCopyService(
            service._sessions,
            transport,  # type: ignore[arg-type]
            service._checkpoints._state_store,
            tmp_path / "control" / "data",
            transfer_gateway=service._transfer_gateway,
            object_store=service._object_store,
            max_transfer_payload_bytes=10_000_000,
            max_transfer_payload_store_bytes=10_000_000,
        )
        try:
            again = await asyncio.wait_for(another.copy(**args), 12)
            assert again.transport == "peer_direct"
            assert transport.calls.count("transfer.peer_send") == 1
            assert target.read_bytes() == b"user edit"
        finally:
            await another.aclose()


@pytest.mark.asyncio
async def test_peer_lost_two_commit_acks_keeps_archive_and_recovers(
    tmp_path: Path, monkeypatch
):
    async with _real_peer_copy(tmp_path, monkeypatch, "dir") as (
        service,
        transport,
        args,
        destination,
    ):
        transport.drop_finish_ack = True
        transport.drop_recovery_ack = True
        with pytest.raises(RuntimeError, match="finish ack lost"):
            await asyncio.wait_for(service.copy(**args), 12)
        checkpoint = service._checkpoints.load(args["transfer_id"])
        assert checkpoint is not None
        assert checkpoint.last_known_step == "importing"
        assert transport.archive_path is not None
        assert transport.archive_path.is_file()
        assert not (destination / "output").exists()
        result = await asyncio.wait_for(service.copy(**args), 12)
        assert result.transport == "peer_direct"
        assert (destination / "output" / "entry.txt").read_text() == "original"
        assert transport.calls.count("transfer.peer_send") == 1
