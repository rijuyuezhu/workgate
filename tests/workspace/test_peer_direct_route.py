"""Control selection and fail-closed fallback for trusted-overlay session_copy."""

import hashlib
from types import SimpleNamespace

import pytest

from tests.workspace.test_session_copy import (
    _fresh_cross_executor_service,
    _ObjectStoreStub,
)
from workgate.protocol.executor import (
    ExecutorResult,
    OperationError,
    TrustedOverlay,
)
from workgate.protocol.ids import new_command_id


def _setup_peer(
    transport,
    *,
    tailnet_match=True,
    send_failure=False,
    cancel_failure=False,
    bad_status=False,
    finish_failure=False,
    prepare_failure=False,
    cancel_noop=False,
    endpoint_mismatch=False,
):
    tailnet = TrustedOverlay(
        provider="tailscale", tailnet_id="test", ipv4="100.100.1.1"
    )
    unrelated = TrustedOverlay(
        provider="tailscale", tailnet_id="unrelated", ipv4="100.101.1.1"
    )

    async def inventory(executor_id):
        overlay = (
            unrelated
            if executor_id == transport.destination_executor_id
            and not tailnet_match
            else tailnet
        )
        return SimpleNamespace(overlay=overlay)

    transport.inventory = inventory
    original = transport.call
    payload = (
        transport.payload
        if transport.source_kind == "file"
        else transport.archive
    )
    assert payload is not None

    async def call(executor_id, op, args=None, **kwargs):
        if op.startswith("transfer.peer_"):
            transport.calls.append((executor_id, op))
            values = args or {}
            if op == "transfer.peer_prepare":
                if prepare_failure:
                    return ExecutorResult(
                        id=new_command_id(),
                        ok=False,
                        error=OperationError(
                            code="operation_failed",
                            message="overlay unavailable",
                        ),
                    )
                result = {
                    "host": "100.100.1.2"
                    if endpoint_mismatch
                    else "100.100.1.1",
                    "port": 25001,
                    "token": "A" * 43,
                    "transfer_id": values["transfer_id"],
                }
            elif op == "transfer.peer_send":
                if send_failure:
                    return ExecutorResult(
                        id=new_command_id(),
                        ok=False,
                        error=OperationError(
                            code="operation_failed", message="peer unreachable"
                        ),
                    )
                transport.destination[:] = payload
                result = {
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "transfer_id": values["transfer_id"],
                }
            elif op == "transfer.peer_status":
                result = {"state": "pending" if bad_status else "received"}
            else:
                if cancel_failure:
                    return ExecutorResult(
                        id=new_command_id(),
                        ok=False,
                        error=OperationError(
                            code="operation_failed", message="cleanup failed"
                        ),
                    )
                transport.destination.clear() if send_failure or bad_status else None
                result = {"cancelled": not cancel_noop}
            return ExecutorResult.model_validate(
                {"id": new_command_id(), "ok": True, "result": result}
            )
        if op == "transfer_finish_write" and finish_failure:
            transport.calls.append((executor_id, op))
            return ExecutorResult(
                id=new_command_id(),
                ok=False,
                error=OperationError(
                    code="operation_failed", message="commit uncertain"
                ),
            )
        return await original(executor_id, op, args, **kwargs)

    transport.call = call


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "dir"])
async def test_automatically_prefers_peer_and_bypasses_other_byte_routes(
    tmp_path, kind
):
    payload = b"peer payload" * 37
    archive = b"packed archive" * 23 if kind == "dir" else None
    obj = _ObjectStoreStub()
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path,
        source_kind=kind,
        payload=payload,
        archive=archive,
        object_store=obj,
    )
    _setup_peer(transport)
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
        kind=kind,
    )
    assert result.transport == "peer_direct"
    assert result.fallbacks == []
    assert obj.begun == 0
    assert bytes(transport.destination) == (
        archive if archive is not None else payload
    )
    ops = [op for _executor, op in transport.calls]
    assert "transfer.peer_send" in ops
    assert "transfer_finish_write" in ops
    assert "transfer.http_upload" not in ops
    assert "transfer.url_upload" not in ops
    if kind == "dir":
        assert ops.count("transfer_pack_dir") == 1
        assert "transfer_unpack_archive" in ops
        assert archive is not None
        assert result.archive_bytes == len(archive)
    else:
        assert result.bytes == len(payload)


@pytest.mark.asyncio
async def test_direct_unreachable_falls_back_to_object_store(tmp_path):
    data = b"fallback to object store"
    obj = _ObjectStoreStub()
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=data, object_store=obj
    )
    _setup_peer(transport, send_failure=True)
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
    )
    assert result.transport == "object_store"
    assert result.fallbacks == ["peer_direct:send"]
    ops = [op for _executor, op in transport.calls]
    assert ops.index("transfer.peer_cancel") < ops.index("transfer.url_upload")
    assert bytes(transport.destination) == data


@pytest.mark.asyncio
async def test_direct_unreachable_falls_back_to_control(tmp_path):
    data = b"fallback to control"
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=data
    )
    _setup_peer(transport, send_failure=True)
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
    )
    assert result.transport == "control_relay"
    assert result.fallbacks == ["peer_direct:send"]
    ops = [op for _executor, op in transport.calls]
    assert ops.index("transfer.peer_cancel") < ops.index("transfer.http_upload")
    assert bytes(transport.destination) == data


@pytest.mark.asyncio
async def test_incompatible_tailnets_do_not_attempt_peer(tmp_path):
    data = b"unrelated networks"
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=data
    )
    _setup_peer(transport, tailnet_match=False)
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
    )
    assert result.transport == "control_relay"
    assert not any(op.startswith("transfer.peer_") for _, op in transport.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["send", "status"])
async def test_failed_peer_abort_blocks_any_fallback(tmp_path, reason):
    data = b"uncertain cleanup"
    obj = _ObjectStoreStub()
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=data, object_store=obj
    )
    _setup_peer(
        transport,
        send_failure=reason == "send",
        bad_status=reason == "status",
        cancel_failure=True,
    )
    with pytest.raises(
        RuntimeError, match="cancellation could not be confirmed"
    ):
        await service.copy(
            src_session_id=str(src.session_id),
            src_path="source",
            dst_session_id=str(dst.session_id),
            dst_path="destination",
        )
    assert obj.begun == 0
    assert "transfer.http_upload" not in [op for _, op in transport.calls]


@pytest.mark.asyncio
async def test_uncertain_peer_commit_never_falls_back(tmp_path):
    data = b"commit may already have happened"
    obj = _ObjectStoreStub()
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=data, object_store=obj
    )
    _setup_peer(transport, finish_failure=True)
    with pytest.raises(RuntimeError, match="transfer_finish_write"):
        await service.copy(
            src_session_id=str(src.session_id),
            src_path="source",
            dst_session_id=str(dst.session_id),
            dst_path="destination",
        )
    assert obj.begun == 0
    assert "transfer.http_upload" not in [op for _, op in transport.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["object", "control"])
async def test_unavailable_receiver_preparation_falls_back(tmp_path, route):
    data = b"overlay exited after hello"
    obj = _ObjectStoreStub() if route == "object" else None
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=data, object_store=obj
    )
    _setup_peer(transport, prepare_failure=True, cancel_noop=True)
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
    )
    assert result.transport == (
        "object_store" if route == "object" else "control_relay"
    )
    assert result.fallbacks == ["peer_direct:prepare"]
    assert "transfer.peer_send" not in [op for _, op in transport.calls]


@pytest.mark.asyncio
async def test_receiver_endpoint_must_match_hello(tmp_path):
    data = b"unexpected overlay peer"
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=data
    )
    _setup_peer(transport, endpoint_mismatch=True)
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
    )
    assert result.transport == "control_relay"
    assert result.fallbacks == ["peer_direct:prepare"]
    assert "transfer.peer_send" not in [op for _, op in transport.calls]
    assert "transfer.peer_cancel" in [op for _, op in transport.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["transfer.peer_prepare", "transfer.peer_send"]
)
async def test_control_copy_cancellation_revokes_offered_peer_receiver(
    tmp_path, stage
):
    import asyncio

    payload = b"cancelled transfer"
    obj = _ObjectStoreStub()
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=payload, object_store=obj
    )
    _setup_peer(transport)
    original_call = transport.call
    entered = asyncio.Event()

    async def paused_call(executor_id, op, args=None, **kwargs):
        if op == stage:
            transport.calls.append((executor_id, op))
            entered.set()
            await asyncio.Event().wait()
        return await original_call(executor_id, op, args, **kwargs)

    transport.call = paused_call
    task = asyncio.create_task(
        service.copy(
            src_session_id=str(src.session_id),
            src_path="source",
            dst_session_id=str(dst.session_id),
            dst_path="destination",
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert "transfer.peer_cancel" in [op for _, op in transport.calls]
        assert obj.begun == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_peer_verification_failure_reconciles_before_fallback(tmp_path):
    payload = b"bad receipt is not a successful copy"
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=payload
    )
    _setup_peer(transport, bad_status=True)
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
    )
    assert result.transport == "control_relay"
    assert result.fallbacks == ["peer_direct:verify"]
    assert bytes(transport.destination) == payload


@pytest.mark.asyncio
async def test_unavailable_peer_then_object_store_then_control(tmp_path):
    from workgate.control.object_store_transfer import (
        ObjectStoreRouteUnavailable,
    )

    payload = b"all routes except relay unavailable"
    obj = _ObjectStoreStub(
        setup_error=ObjectStoreRouteUnavailable("setup", "temporary outage")
    )
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=payload, object_store=obj
    )
    _setup_peer(transport, send_failure=True)
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
    )
    assert result.transport == "control_relay"
    assert result.fallbacks == ["peer_direct:send", "object_store:setup"]
    assert bytes(transport.destination) == payload


@pytest.mark.asyncio
async def test_unavailable_inventory_falls_back_without_peer_attempt(tmp_path):
    payload = b"no overlay inventory"
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=payload
    )
    _setup_peer(transport)

    async def broken_inventory(_executor_id):
        raise OSError("inventory currently unavailable")

    transport.inventory = broken_inventory  # type: ignore[attr-defined]
    result = await service.copy(
        src_session_id=str(src.session_id),
        src_path="source",
        dst_session_id=str(dst.session_id),
        dst_path="destination",
    )
    assert result.transport == "control_relay"
    assert not any(op.startswith("transfer.peer_") for _, op in transport.calls)


@pytest.mark.asyncio
async def test_missing_receiver_after_send_is_not_cleanup_proof(tmp_path):
    data = b"unknown transaction ownership"
    obj = _ObjectStoreStub()
    service, src, dst, transport = _fresh_cross_executor_service(
        tmp_path, source_kind="file", payload=data, object_store=obj
    )
    _setup_peer(transport, send_failure=True, cancel_noop=True)
    with pytest.raises(RuntimeError, match="cancellation was not confirmed"):
        await service.copy(
            src_session_id=str(src.session_id),
            src_path="source",
            dst_session_id=str(dst.session_id),
            dst_path="destination",
        )
    assert obj.begun == 0
    assert "transfer.http_upload" not in [op for _, op in transport.calls]
