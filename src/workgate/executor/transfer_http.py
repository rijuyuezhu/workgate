"""Outbound-only raw HTTP byte transport for private session-copy commands."""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import JsonValue

from ..config.executor import ExecutorConfig
from ..protocol.executor import EXECUTOR_TRANSFER_TOKEN_HEADER
from ..protocol.transfer import normalize_chunk_size
from .profile import ExecutorProfile
from .tool_session.store import ToolSessionStore
from .transfer import (
    TransferContext,
    _open_transfer_source,
    _resolve_temp_path,
    _resolve_transfer_path,
    _stable_source_identity,
    transfer_write_bytes,
)


def _client(profile: ExecutorProfile) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=profile.control_url,
        headers={"Authorization": f"Bearer {profile.credential}"},
        follow_redirects=False,
        trust_env=profile.control_url.lower().startswith("https://"),
        timeout=httpx.Timeout(60.0, read=None, write=None, pool=60.0),
    )


async def upload_to_control(
    profile: ExecutorProfile,
    config: ExecutorConfig,
    store: ToolSessionStore,
    args: dict[str, Any],
) -> dict[str, JsonValue]:
    """Stream one bound source file to a single-claim control capability."""
    session_id = str(args["session_id"])
    store.admit_active_session(session_id)
    context = TransferContext(config, store)
    path = str(args["path"])
    if bool(args.get("_workgate_unbound_temp", False)):
        source = _resolve_temp_path(path, must_exist=True, context=context)
    else:
        source = _resolve_transfer_path(
            path,
            must_exist=True,
            session_id=session_id,
            context=context,
        )
    expected_bytes = int(args["expected_bytes"])
    expected_sha256 = str(args["expected_sha256"])
    chunk_size = normalize_chunk_size(args.get("chunk_size"))
    capability_path = str(args["capability_path"])
    capability_token = str(args["capability_token"])

    handle = _open_transfer_source(source)
    initial_stat = os.fstat(handle.fileno())
    if int(initial_stat.st_size) != expected_bytes:
        handle.close()
        raise ValueError("transfer upload source size changed before streaming")

    digest = hashlib.sha256()
    sent = 0

    async def content() -> AsyncIterator[bytes]:
        nonlocal sent
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, chunk_size)
                if not chunk:
                    break
                sent += len(chunk)
                digest.update(chunk)
                yield chunk
            final_stat = os.fstat(handle.fileno())
            if _stable_source_identity(initial_stat) != _stable_source_identity(
                final_stat
            ):
                raise ValueError(
                    "transfer upload source changed during streaming"
                )
            if sent != expected_bytes or digest.hexdigest() != expected_sha256:
                raise ValueError("transfer upload source integrity mismatch")
        finally:
            handle.close()

    async with _client(profile) as client:
        try:
            response = await client.put(
                capability_path,
                headers={
                    EXECUTOR_TRANSFER_TOKEN_HEADER: capability_token,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(expected_bytes),
                },
                content=content(),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"control transfer upload failed: {type(exc).__name__}"
            ) from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"control transfer upload rejected with HTTP {response.status_code}"
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "control transfer upload returned invalid JSON"
        ) from exc
    if (
        not isinstance(result, dict)
        or int(result.get("bytes", -1)) != expected_bytes
        or str(result.get("sha256") or "") != expected_sha256
    ):
        raise RuntimeError("control transfer upload acknowledgement is invalid")
    return {"bytes": expected_bytes, "sha256": expected_sha256}


async def download_from_control(
    profile: ExecutorProfile,
    config: ExecutorConfig,
    store: ToolSessionStore,
    args: dict[str, Any],
) -> dict[str, JsonValue]:
    """Stream retained control bytes into one existing transactional write."""
    session_id = str(args["session_id"])
    store.admit_active_session(session_id)
    context = TransferContext(config, store)
    path = str(args["path"])
    transfer_id = str(args["transfer_id"])
    unbound_temp = bool(args.get("_workgate_unbound_temp", False))
    expected_bytes = int(args["expected_bytes"])
    chunk_size = normalize_chunk_size(args.get("chunk_size"))
    offset = int(args["offset"])
    if offset < 0 or offset > expected_bytes:
        raise ValueError("transfer download offset is invalid")
    capability_path = str(args["capability_path"])
    capability_token = str(args["capability_token"])
    current = offset

    async with _client(profile) as client:
        try:
            async with client.stream(
                "GET",
                capability_path,
                headers={EXECUTOR_TRANSFER_TOKEN_HEADER: capability_token},
            ) as response:
                if response.status_code != 200:
                    raise RuntimeError(
                        "control transfer download rejected with HTTP "
                        f"{response.status_code}"
                    )
                response_offset = response.headers.get(
                    "x-workgate-transfer-offset"
                )
                if response_offset is None or int(response_offset) != offset:
                    raise RuntimeError(
                        "control transfer download offset acknowledgement is invalid"
                    )
                declared = response.headers.get("content-length")
                if (
                    declared is not None
                    and int(declared) != expected_bytes - offset
                ):
                    raise RuntimeError(
                        "control transfer download length is invalid"
                    )
                async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    if current + len(chunk) > expected_bytes:
                        raise RuntimeError(
                            "control transfer download exceeded expected size"
                        )
                    digest = hashlib.sha256(chunk).hexdigest()
                    await asyncio.to_thread(
                        transfer_write_bytes,
                        path,
                        transfer_id,
                        current,
                        chunk,
                        digest,
                        session_id=None if unbound_temp else session_id,
                        context=context,
                    )
                    current += len(chunk)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"control transfer download failed: {type(exc).__name__}"
            ) from exc

    if current != expected_bytes:
        raise RuntimeError(
            "control transfer download ended before expected size"
        )
    return {"offset": current, "bytes": current - offset}
