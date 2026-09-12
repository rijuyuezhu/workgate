"""Internal transfer operation tool registry for remote-worker file moves."""

import asyncio
from typing import Any, NoReturn

from ...schemas.input_models.session import OptionalSessionIdArg
from ...schemas.input_models.transfer import (
    TransferArchivePathArg,
    TransferChunkSizeArg,
    TransferCleanupArchiveArg,
    TransferCompressionArg,
    TransferDataArg,
    TransferDestinationPathArg,
    TransferExpectedBytesArg,
    TransferIdArg,
    TransferOffsetArg,
    TransferOverwriteArg,
    TransferPathArg,
    TransferSha256Arg,
    TransferSha256EnabledArg,
    TransferSuffixArg,
)
from ...schemas.result_models.transfer import (
    TransferAbortWriteOutput,
    TransferAllocTempPathOutput,
    TransferBeginWriteOutput,
    TransferCopyFileOutput,
    TransferDeleteTempPathOutput,
    TransferFinishWriteOutput,
    TransferPackDirOutput,
    TransferReadChunkOutput,
    TransferStatOutput,
    TransferUnpackArchiveOutput,
    TransferWriteChunkOutput,
)
from ..contracts import HttpToolRoute
from ..declarative import DeclarativeToolRegistry


def _transfer_impl() -> NoReturn:
    """Fail closed unless the final executor composition binds transfer handlers."""
    raise RuntimeError("transfer operations require composed executor services")


class TransferToolRegistry(DeclarativeToolRegistry):
    """Register worker-side transfer primitives without public routes."""

    name = "transfer"
    """Registry group name used for tool-surface organization."""

    def http_routes(self) -> tuple[HttpToolRoute, ...]:
        """Keep raw transfer primitives off the public REST surface."""
        return ()

    def register_mcp(self, *args: Any, **kwargs: Any) -> None:
        """Keep raw transfer primitives off the public MCP surface."""
        return None


transfer_tool = TransferToolRegistry.get_tool_decorator()


@transfer_tool(
    http_method="POST",
    http_path="/tools/transfer_stat",
    annotations="read_only",
)
async def transfer_stat(
    path: TransferPathArg,
    sha256: TransferSha256EnabledArg = True,
    session_id: OptionalSessionIdArg = None,
) -> TransferStatOutput:
    """Return transfer metadata for a file or directory."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_stat, path, sha256, session_id=session_id
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_copy_file")
async def transfer_copy_file(
    source_path: TransferPathArg,
    destination_path: TransferDestinationPathArg,
    overwrite: TransferOverwriteArg = True,
    chunk_size: TransferChunkSizeArg = None,
    source_session_id: OptionalSessionIdArg = None,
    destination_session_id: OptionalSessionIdArg = None,
) -> TransferCopyFileOutput:
    """Stream a file between two paths on the same worker."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_copy_file,
        source_path,
        destination_path,
        overwrite,
        chunk_size,
        source_session_id=source_session_id,
        destination_session_id=destination_session_id,
    )


@transfer_tool(
    http_method="POST",
    http_path="/tools/transfer_read_chunk",
    annotations="read_only",
)
async def transfer_read_chunk(
    path: TransferPathArg,
    offset: TransferOffsetArg = 0,
    chunk_size: TransferChunkSizeArg = None,
    session_id: OptionalSessionIdArg = None,
) -> TransferReadChunkOutput:
    """Read one base64-encoded binary chunk from a file."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_read_chunk,
        path,
        offset,
        chunk_size,
        session_id=session_id,
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_begin_write")
async def transfer_begin_write(
    path: TransferPathArg,
    overwrite: TransferOverwriteArg = True,
    expected_bytes: TransferExpectedBytesArg = None,
    session_id: OptionalSessionIdArg = None,
) -> TransferBeginWriteOutput:
    """Start an atomic chunked file write and return a transfer id."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_begin_write,
        path,
        overwrite,
        expected_bytes,
        session_id=session_id,
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_write_chunk")
async def transfer_write_chunk(
    path: TransferPathArg,
    transfer_id: TransferIdArg,
    offset: TransferOffsetArg,
    data_b64: TransferDataArg,
    expected_sha256: TransferSha256Arg = None,
    session_id: OptionalSessionIdArg = None,
) -> TransferWriteChunkOutput:
    """Write one base64-encoded chunk into an active transfer."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_write_chunk,
        path,
        transfer_id,
        offset,
        data_b64,
        expected_sha256,
        session_id=session_id,
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_finish_write")
async def transfer_finish_write(
    path: TransferPathArg,
    transfer_id: TransferIdArg,
    expected_bytes: TransferExpectedBytesArg = None,
    expected_sha256: TransferSha256Arg = None,
    session_id: OptionalSessionIdArg = None,
) -> TransferFinishWriteOutput:
    """Validate and atomically finish an active transfer."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_finish_write,
        path,
        transfer_id,
        expected_bytes,
        expected_sha256,
        session_id=session_id,
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_abort_write")
async def transfer_abort_write(
    path: TransferPathArg,
    transfer_id: TransferIdArg,
    session_id: OptionalSessionIdArg = None,
) -> TransferAbortWriteOutput:
    """Abort an active transfer and remove its temporary file."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_abort_write,
        path,
        transfer_id,
        session_id=session_id,
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_alloc_temp_path")
async def transfer_alloc_temp_path(
    suffix: TransferSuffixArg = ".bin",
    session_id: OptionalSessionIdArg = None,
) -> TransferAllocTempPathOutput:
    """Allocate a safe temporary path for transfer archives."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_alloc_temp_path, suffix, session_id=session_id
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_pack_dir")
async def transfer_pack_dir(
    path: TransferPathArg,
    compression: TransferCompressionArg = "gz",
    session_id: OptionalSessionIdArg = None,
) -> TransferPackDirOutput:
    """Pack a directory into a temporary tar archive."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_pack_dir,
        path,
        compression,
        session_id=session_id,
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_unpack_archive")
async def transfer_unpack_archive(
    archive_path: TransferArchivePathArg,
    dst_path: TransferDestinationPathArg,
    overwrite: TransferOverwriteArg = True,
    cleanup_archive: TransferCleanupArchiveArg = True,
    session_id: OptionalSessionIdArg = None,
) -> TransferUnpackArchiveOutput:
    """Safely unpack a transfer archive into a destination directory."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_unpack_archive,
        archive_path,
        dst_path,
        overwrite,
        cleanup_archive,
        session_id=session_id,
    )


@transfer_tool(http_method="POST", http_path="/tools/transfer_delete_temp_path")
async def transfer_delete_temp_path(
    path: TransferPathArg,
) -> TransferDeleteTempPathOutput:
    """Delete a transfer scratch file under the configured temp directory."""
    return await asyncio.to_thread(
        _transfer_impl().transfer_delete_temp_path, path
    )
