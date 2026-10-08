"""Typed input annotations for session-scoped browser automation."""

from typing import Annotated, Literal, NotRequired

from pydantic import Field
from typing_extensions import TypedDict

BrowserSessionActionArg = Annotated[
    Literal["start", "list", "close"],
    Field(
        description="Browser lifecycle action inside the owning Workgate session."
    ),
]
BrowserSessionIdArg = Annotated[
    str | None,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^browser_[A-Za-z0-9_-]{12,}$",
        description="Opaque browser_session_id returned by browser_session action=start.",
    ),
]
RequiredBrowserSessionIdArg = Annotated[
    str,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^browser_[A-Za-z0-9_-]{12,}$",
        description="Opaque browser_session_id returned by browser_session action=start.",
    ),
]
BrowserPageIdArg = Annotated[
    str | None,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^page_[A-Za-z0-9_-]{8,}$",
        description="Optional browser page_id; omit to use the most recently active page.",
    ),
]
BrowserProfileIdArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Optional case-insensitive durable Chromium profile name on the owning executor. Reuse the same explicit name later to reuse browser authentication state.",
    ),
]
BrowserStorageStatePathArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=4096,
        description="Optional relative JSON path in the owning Workgate session workdir used to import or export Playwright storage state.",
    ),
]
BrowserUrlArg = Annotated[
    str | None,
    Field(
        max_length=8192,
        description="Optional http(s) URL to open. file:, data:, javascript:, and other schemes are rejected.",
    ),
]
BrowserHeadlessArg = Annotated[
    bool,
    Field(
        description="Run Chromium headlessly. Headless mode is recommended on executors."
    ),
]
BrowserViewportWidthArg = Annotated[
    int,
    Field(ge=320, le=7680, description="Browser viewport width in CSS pixels."),
]
BrowserViewportHeightArg = Annotated[
    int,
    Field(
        ge=240, le=4320, description="Browser viewport height in CSS pixels."
    ),
]
BrowserWaitUntilArg = Annotated[
    Literal["load", "domcontentloaded", "networkidle", "commit"],
    Field(description="Playwright navigation readiness condition."),
]
BrowserIncludeTextArg = Annotated[
    bool,
    Field(description="Include bounded visible body text in the snapshot."),
]
BrowserMaxTextCharsArg = Annotated[
    int,
    Field(
        ge=0,
        le=100_000,
        description="Maximum visible-text characters returned by one snapshot.",
    ),
]
BrowserMaxElementsArg = Annotated[
    int,
    Field(
        ge=1,
        le=200,
        description="Maximum visible interactive elements returned by one snapshot.",
    ),
]
BrowserScreenshotArg = Annotated[
    bool,
    Field(
        description="Capture a PNG screenshot by default; set false to skip."
    ),
]
BrowserScreenshotPathArg = Annotated[
    str | None,
    Field(
        max_length=4096,
        description="Optional new .png path. Relative values resolve from the owning session workdir; existing files are never overwritten.",
    ),
]
BrowserFullPageArg = Annotated[
    bool,
    Field(
        description="Capture the full scrollable page when screenshots are enabled."
    ),
]
BrowserTarget = Annotated[
    str,
    Field(
        min_length=1,
        max_length=4096,
        description="Snapshot ref such as e1 (preferred), or a CSS selector matching one element.",
    ),
]
BrowserActionUrl = Annotated[
    str,
    Field(
        min_length=1,
        max_length=8192,
        description="Browser action URL or URL pattern.",
    ),
]
BrowserActionValue = Annotated[
    str,
    Field(
        max_length=100_000, description="Bounded value entered into the page."
    ),
]
BrowserSelectValues = Annotated[
    list[BrowserActionValue],
    Field(
        max_length=100,
        description="Bounded option values selected in one action.",
    ),
]


class BrowserNavigateAction(TypedDict, closed=True):
    action: Literal["navigate"]
    url: BrowserActionUrl
    wait_until: NotRequired[BrowserWaitUntilArg]


class BrowserNewPageAction(TypedDict, closed=True):
    action: Literal["new_page"]
    url: NotRequired[BrowserActionUrl]


class BrowserClosePageAction(TypedDict, closed=True):
    action: Literal["close_page"]


class BrowserTargetAction(TypedDict, closed=True):
    action: Literal["click", "check", "uncheck", "hover"]
    target: BrowserTarget


class BrowserValueAction(TypedDict, closed=True):
    action: Literal["fill", "type"]
    target: BrowserTarget
    value: BrowserActionValue


class BrowserSelectAction(TypedDict, closed=True):
    action: Literal["select"]
    target: BrowserTarget
    value: BrowserActionValue | BrowserSelectValues


class BrowserPressAction(TypedDict, closed=True):
    action: Literal["press"]
    target: BrowserTarget
    key: Annotated[str, Field(min_length=1, max_length=128)]


class BrowserWaitAction(TypedDict, closed=True):
    action: Literal["wait"]
    ms: NotRequired[Annotated[int, Field(ge=0, le=30_000)]]


class BrowserWaitForTextAction(TypedDict, closed=True):
    action: Literal["wait_for_text"]
    text: Annotated[str, Field(min_length=1, max_length=4096)]


class BrowserWaitForUrlAction(TypedDict, closed=True):
    action: Literal["wait_for_url"]
    url: BrowserActionUrl


BrowserAction = Annotated[
    BrowserNavigateAction
    | BrowserNewPageAction
    | BrowserClosePageAction
    | BrowserTargetAction
    | BrowserValueAction
    | BrowserSelectAction
    | BrowserPressAction
    | BrowserWaitAction
    | BrowserWaitForTextAction
    | BrowserWaitForUrlAction,
    Field(discriminator="action"),
]
BrowserActionsArg = Annotated[
    list[BrowserAction],
    Field(
        min_length=1,
        max_length=50,
        description="Ordered high-level actions using snapshot refs or CSS selectors for element targets.",
    ),
]
BrowserTimeoutMsArg = Annotated[
    int,
    Field(
        ge=1,
        le=120_000,
        description="Per-action Playwright timeout in milliseconds.",
    ),
]
