"""Typed input annotations for executor-side transfer tools."""

from typing import Annotated

from pydantic import Field

TransferPathArg = Annotated[
    str,
    Field(
        description="File or directory path for an executor-side transfer operation."
    ),
]
TransferArchivePathArg = Annotated[
    str,
    Field(description="Temporary archive path produced by transfer_pack_dir."),
]
TransferDestinationPathArg = Annotated[
    str,
    Field(
        description="Destination path for unpacking or writing transfer data."
    ),
]
TransferIdArg = Annotated[
    str,
    Field(description="Opaque transfer id returned by transfer_begin_write."),
]
TransferOffsetArg = Annotated[
    int,
    Field(description="Byte offset for a chunked transfer operation."),
]
TransferChunkSizeArg = Annotated[
    int | None,
    Field(
        description="Optional chunk size in bytes. Omit to use the server default."
    ),
]
TransferDataArg = Annotated[
    str,
    Field(description="Encoded chunk data to write."),
]
TransferSha256Arg = Annotated[
    str | None,
    Field(description="Optional expected SHA-256 hex digest for validation."),
]
TransferSha256EnabledArg = Annotated[
    bool,
    Field(
        description="Whether to compute SHA-256 metadata for the target path."
    ),
]
TransferOverwriteArg = Annotated[
    bool,
    Field(description="Whether an existing destination may be replaced."),
]
TransferExpectedBytesArg = Annotated[
    int | None,
    Field(description="Optional expected byte length for validation."),
]
TransferSuffixArg = Annotated[
    str,
    Field(
        description="Filename suffix for an allocated temporary transfer path."
    ),
]
TransferCompressionArg = Annotated[
    str,
    Field(description="Compression mode for a packed directory archive."),
]
TransferCleanupArchiveArg = Annotated[
    bool,
    Field(
        description="Whether to remove the archive after unpacking succeeds."
    ),
]
