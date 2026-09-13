"""Typed input annotations for tokenized download-link tools."""

from typing import Annotated

from pydantic import Field

DownloadPathArg = Annotated[
    str,
    Field(
        description="Existing regular file path to expose through a temporary tokenized download URL."
    ),
]
DownloadTtlArg = Annotated[
    int | None,
    Field(
        description="Optional link lifetime in seconds. Omit to use the configured default; values above the configured cap are clamped."
    ),
]
DownloadFilenameArg = Annotated[
    str | None,
    Field(
        description="Optional browser download filename. Path components are stripped."
    ),
]
MaxDownloadsArg = Annotated[
    int | None,
    Field(
        description="Optional maximum number of downloads. Use 0 for unlimited; omit to use the configured default."
    ),
]
InlineDownloadArg = Annotated[
    bool,
    Field(
        description="Whether the browser should render the snapshot inline instead of downloading it as an attachment. Defaults to false."
    ),
]
IncludeExpiredArg = Annotated[
    bool,
    Field(
        description="Whether to include expired or exhausted links in the listing."
    ),
]
DownloadLinkIdArg = Annotated[
    str,
    Field(
        description="Non-secret link_id returned by create_file_link or list_file_links."
    ),
]
