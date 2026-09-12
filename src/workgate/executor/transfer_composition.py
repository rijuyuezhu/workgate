"""Explicit executor composition for transactional transfer primitives."""

import asyncio
from typing import Any

from .config import ExecutorConfig
from .dispatch import ExecutorHandler
from .tool_session.store import ToolSessionStore
from .transfer import (
    TransferContext,
    transfer_abort_write,
    transfer_alloc_temp_path,
    transfer_begin_write,
    transfer_copy_file,
    transfer_delete_temp_path,
    transfer_finish_write,
    transfer_pack_dir,
    transfer_read_chunk,
    transfer_stat,
    transfer_unpack_archive,
    transfer_write_chunk,
)


def _transfer_session_id(
    args: dict[str, Any],
    *,
    session_key: str = "session_id",
    workdir_key: str = "workdir",
) -> Any:
    """Prefer an immutable workdir binding over a mutable session lookup."""
    if bool(args.get("_workgate_unbound_temp", False)):
        return None
    return None if args.get(workdir_key) is not None else args.get(session_key)


def _admit_unbound_activity(
    store: ToolSessionStore, args: dict[str, Any]
) -> None:
    """Admit the command session when transfer resolution intentionally bypasses it."""
    session_id = args.get("session_id")
    if session_id is not None:
        store.admit_active_session(str(session_id))


def build_transfer_handlers(
    config: ExecutorConfig, store: ToolSessionStore
) -> dict[str, ExecutorHandler]:
    """Build final transfer handlers under explicit executor authority."""
    context = TransferContext(config, store)

    async def stat(args: dict[str, Any]) -> Any:
        session_id = _transfer_session_id(args)
        if session_id is None:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_stat,
            str(args["path"]),
            bool(args.get("sha256", True)),
            session_id=session_id,
            workdir=args.get("workdir"),
            context=context,
        )

    async def copy_file(args: dict[str, Any]) -> Any:
        source_session_id = _transfer_session_id(
            args,
            session_key="source_session_id",
            workdir_key="source_workdir",
        )
        destination_session_id = _transfer_session_id(
            args,
            session_key="destination_session_id",
            workdir_key="destination_workdir",
        )
        command_session_id = args.get("session_id")
        if command_session_id is not None and str(command_session_id) not in {
            str(value)
            for value in (source_session_id, destination_session_id)
            if value is not None
        }:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_copy_file,
            str(args["source_path"]),
            str(args["destination_path"]),
            bool(args.get("overwrite", True)),
            args.get("chunk_size"),
            source_session_id=source_session_id,
            destination_session_id=destination_session_id,
            source_workdir=args.get("source_workdir"),
            destination_workdir=args.get("destination_workdir"),
            context=context,
        )

    async def read_chunk(args: dict[str, Any]) -> Any:
        session_id = _transfer_session_id(args)
        if session_id is None:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_read_chunk,
            str(args["path"]),
            int(args.get("offset") or 0),
            args.get("chunk_size"),
            session_id=session_id,
            workdir=args.get("workdir"),
            context=context,
        )

    async def begin_write(args: dict[str, Any]) -> Any:
        session_id = _transfer_session_id(args)
        if session_id is None:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_begin_write,
            str(args["path"]),
            bool(args.get("overwrite", True)),
            args.get("expected_bytes"),
            session_id=session_id,
            workdir=args.get("workdir"),
            context=context,
        )

    async def write_chunk(args: dict[str, Any]) -> Any:
        session_id = _transfer_session_id(args)
        if session_id is None:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_write_chunk,
            str(args["path"]),
            str(args["transfer_id"]),
            int(args["offset"]),
            str(args["data_b64"]),
            args.get("expected_sha256"),
            session_id=session_id,
            workdir=args.get("workdir"),
            context=context,
        )

    async def finish_write(args: dict[str, Any]) -> Any:
        session_id = _transfer_session_id(args)
        if session_id is None:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_finish_write,
            str(args["path"]),
            str(args["transfer_id"]),
            args.get("expected_bytes"),
            args.get("expected_sha256"),
            session_id=session_id,
            workdir=args.get("workdir"),
            context=context,
        )

    async def abort_write(args: dict[str, Any]) -> Any:
        session_id = _transfer_session_id(args)
        if session_id is None:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_abort_write,
            str(args["path"]),
            str(args["transfer_id"]),
            session_id=session_id,
            workdir=args.get("workdir"),
            context=context,
        )

    async def alloc_temp_path(args: dict[str, Any]) -> Any:
        _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_alloc_temp_path,
            str(args.get("suffix") or ".bin"),
            session_id=args.get("session_id"),
            context=context,
        )

    async def pack_dir(args: dict[str, Any]) -> Any:
        session_id = _transfer_session_id(args)
        if session_id is None:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_pack_dir,
            str(args["path"]),
            str(args.get("compression") or "gz"),
            session_id=session_id,
            workdir=args.get("workdir"),
            context=context,
        )

    async def unpack_archive(args: dict[str, Any]) -> Any:
        session_id = _transfer_session_id(args)
        if session_id is None:
            _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_unpack_archive,
            str(args["archive_path"]),
            str(args["dst_path"]),
            bool(args.get("overwrite", True)),
            bool(args.get("cleanup_archive", True)),
            session_id=session_id,
            workdir=args.get("workdir"),
            context=context,
        )

    async def delete_temp_path(args: dict[str, Any]) -> Any:
        _admit_unbound_activity(store, args)
        return await asyncio.to_thread(
            transfer_delete_temp_path,
            str(args["path"]),
            context=context,
        )

    return {
        "transfer_abort_write": abort_write,
        "transfer_alloc_temp_path": alloc_temp_path,
        "transfer_begin_write": begin_write,
        "transfer_copy_file": copy_file,
        "transfer_delete_temp_path": delete_temp_path,
        "transfer_finish_write": finish_write,
        "transfer_pack_dir": pack_dir,
        "transfer_read_chunk": read_chunk,
        "transfer_stat": stat,
        "transfer_unpack_archive": unpack_archive,
        "transfer_write_chunk": write_chunk,
    }
