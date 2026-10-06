"""Outbound-only raw HTTP byte transport for private session-copy commands."""

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from ..config.executor import ExecutorConfig
from ..protocol.executor import EXECUTOR_TRANSFER_TOKEN_HEADER
from ..protocol.transfer import normalize_chunk_size
from .errors import ExecutorOperationFailure
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

_MAX_EXTERNAL_URL_CHARS = 16_384
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


def _control_client(profile: ExecutorProfile) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=profile.control_url,
        headers={"Authorization": f"Bearer {profile.credential}"},
        follow_redirects=False,
        trust_env=profile.control_url.lower().startswith("https://"),
        timeout=httpx.Timeout(60.0, read=None, write=None, pool=60.0),
    )


def _external_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(60.0, read=None, write=None, pool=60.0),
    )


def _validate_external_url(value: object) -> str:
    url = str(value or "")
    if len(url) > _MAX_EXTERNAL_URL_CHARS:
        raise ValueError("external transfer URL is too long")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(
            "external transfer URL must be an absolute HTTP(S) URL "
            "without embedded credentials or fragment"
        )
    return url


def _route_unavailable(message: str) -> ExecutorOperationFailure:
    return ExecutorOperationFailure("transfer_route_unavailable", message)


class _ResponseStreamError(RuntimeError):
    """The remote byte response cannot satisfy the expected transfer framing."""


def _open_source(
    config: ExecutorConfig,
    store: ToolSessionStore,
    args: dict[str, Any],
):
    session_id = str(args["session_id"])
    store.admit_active_session(session_id)
    context = TransferContext(config, store)
    path = str(args["path"])
    unbound_temp = bool(args.get("_workgate_unbound_temp", False))
    if unbound_temp:
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
    handle = _open_transfer_source(source)
    initial_stat = os.fstat(handle.fileno())
    if int(initial_stat.st_size) != expected_bytes:
        handle.close()
        raise ValueError("transfer upload source size changed before streaming")
    return (
        handle,
        initial_stat,
        expected_bytes,
        expected_sha256,
        chunk_size,
    )


async def _source_content(
    handle,
    initial_stat,
    *,
    expected_bytes: int,
    expected_sha256: str,
    chunk_size: int,
) -> AsyncIterator[bytes]:
    digest = hashlib.sha256()
    sent = 0
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
            raise ValueError("transfer upload source changed during streaming")
        if sent != expected_bytes or digest.hexdigest() != expected_sha256:
            raise ValueError("transfer upload source integrity mismatch")
    finally:
        handle.close()


async def _write_response(
    response: httpx.Response,
    *,
    config: ExecutorConfig,
    store: ToolSessionStore,
    args: dict[str, Any],
    offset: int,
    expected_bytes: int,
    chunk_size: int,
) -> dict[str, JsonValue]:
    session_id = str(args["session_id"])
    context = TransferContext(config, store)
    path = str(args["path"])
    transfer_id = str(args["transfer_id"])
    unbound_temp = bool(args.get("_workgate_unbound_temp", False))
    current = offset
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError as exc:
            raise _ResponseStreamError(
                "external transfer download length is invalid"
            ) from exc
        if declared_bytes != expected_bytes - offset:
            raise _ResponseStreamError(
                "external transfer download length is invalid"
            )
    async for chunk in response.aiter_bytes(chunk_size=chunk_size):
        if not chunk:
            continue
        if current + len(chunk) > expected_bytes:
            raise _ResponseStreamError(
                "external transfer download exceeded expected size"
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
    if current != expected_bytes:
        raise _ResponseStreamError(
            "external transfer download ended before expected size"
        )
    return {"offset": current, "bytes": current - offset}


async def upload_to_control(
    profile: ExecutorProfile,
    config: ExecutorConfig,
    store: ToolSessionStore,
    args: dict[str, Any],
) -> dict[str, JsonValue]:
    """Stream one bound source file to a single-claim control capability."""
    (
        handle,
        initial_stat,
        expected_bytes,
        expected_sha256,
        chunk_size,
    ) = _open_source(config, store, args)
    capability_path = str(args["capability_path"])
    capability_token = str(args["capability_token"])

    async with _control_client(profile) as client:
        try:
            response = await client.put(
                capability_path,
                headers={
                    EXECUTOR_TRANSFER_TOKEN_HEADER: capability_token,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(expected_bytes),
                },
                content=_source_content(
                    handle,
                    initial_stat,
                    expected_bytes=expected_bytes,
                    expected_sha256=expected_sha256,
                    chunk_size=chunk_size,
                ),
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


async def upload_to_url(
    config: ExecutorConfig,
    store: ToolSessionStore,
    args: dict[str, Any],
) -> dict[str, JsonValue]:
    """Stream one bound source file to a control-issued presigned URL."""
    try:
        url = _validate_external_url(args.get("url"))
    except ValueError as exc:
        raise _route_unavailable("object-store upload URL is invalid") from exc
    (
        handle,
        initial_stat,
        expected_bytes,
        expected_sha256,
        chunk_size,
    ) = _open_source(config, store, args)
    async with _external_client() as client:
        try:
            response = await client.put(
                url,
                headers={"Content-Length": str(expected_bytes)},
                content=_source_content(
                    handle,
                    initial_stat,
                    expected_bytes=expected_bytes,
                    expected_sha256=expected_sha256,
                    chunk_size=chunk_size,
                ),
            )
        except httpx.HTTPError as exc:
            raise _route_unavailable(
                f"object-store upload failed: {type(exc).__name__}"
            ) from exc
    if not 200 <= response.status_code < 300:
        raise _route_unavailable(
            f"object-store upload rejected with HTTP {response.status_code}"
        )
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
    expected_bytes = int(args["expected_bytes"])
    chunk_size = normalize_chunk_size(args.get("chunk_size"))
    offset = int(args["offset"])
    if offset < 0 or offset > expected_bytes:
        raise ValueError("transfer download offset is invalid")
    capability_path = str(args["capability_path"])
    capability_token = str(args["capability_token"])

    async with _control_client(profile) as client:
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
                return await _write_response(
                    response,
                    config=config,
                    store=store,
                    args=args,
                    offset=offset,
                    expected_bytes=expected_bytes,
                    chunk_size=chunk_size,
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"control transfer download failed: {type(exc).__name__}"
            ) from exc


async def download_from_url(
    config: ExecutorConfig,
    store: ToolSessionStore,
    args: dict[str, Any],
) -> dict[str, JsonValue]:
    """Stream one presigned object into an existing transactional write."""
    session_id = str(args["session_id"])
    store.admit_active_session(session_id)
    try:
        url = _validate_external_url(args.get("url"))
    except ValueError as exc:
        raise _route_unavailable(
            "object-store download URL is invalid"
        ) from exc
    expected_bytes = int(args["expected_bytes"])
    chunk_size = normalize_chunk_size(args.get("chunk_size"))
    offset = int(args["offset"])
    if offset < 0 or offset > expected_bytes:
        raise ValueError("transfer download offset is invalid")

    headers: dict[str, str] = {}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    async with _external_client() as client:
        try:
            async with client.stream("GET", url, headers=headers) as response:
                if offset:
                    if response.status_code != 206:
                        raise _route_unavailable(
                            "object-store resume requires HTTP 206"
                        )
                elif response.status_code not in {200, 206}:
                    raise _route_unavailable(
                        "object-store download rejected with HTTP "
                        f"{response.status_code}"
                    )
                if response.status_code == 206:
                    raw_range = response.headers.get("content-range", "")
                    matched = _CONTENT_RANGE_RE.fullmatch(raw_range)
                    if matched is None:
                        raise _route_unavailable(
                            "object-store download Content-Range is invalid"
                        )
                    start, _end, total = matched.groups()
                    if int(start) != offset or (
                        total != "*" and int(total) != expected_bytes
                    ):
                        raise _route_unavailable(
                            "object-store download range does not match "
                            "the destination transaction"
                        )
                try:
                    return await _write_response(
                        response,
                        config=config,
                        store=store,
                        args=args,
                        offset=offset,
                        expected_bytes=expected_bytes,
                        chunk_size=chunk_size,
                    )
                except _ResponseStreamError as exc:
                    raise _route_unavailable(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise _route_unavailable(
                f"object-store download failed: {type(exc).__name__}"
            ) from exc
