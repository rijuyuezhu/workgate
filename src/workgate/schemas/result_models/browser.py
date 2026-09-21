"""Typed structured outputs for browser automation."""

from pydantic import BaseModel, Field


class BrowserPageSummary(BaseModel):
    """One live page inside a browser session."""

    page_id: str
    title: str
    url: str


class BrowserSessionSummary(BaseModel):
    """One ephemeral browser owned by a Workgate session."""

    browser_session_id: str
    current_page_id: str | None = None
    pages: list[BrowserPageSummary] = Field(default_factory=list)
    created_at: float
    last_used_at: float


class BrowserSessionOutput(BaseModel):
    """Lifecycle result for browser_session."""

    browser_session_id: str | None = None
    current_page_id: str | None = None
    pages: list[BrowserPageSummary] = Field(default_factory=list)
    created_at: float | None = None
    last_used_at: float | None = None
    sessions: list[BrowserSessionSummary] | None = None
    cleanup_pending: list[str] | None = None
    closed: bool | None = None


class BrowserInteractiveElement(BaseModel):
    """Bounded interactive element metadata with a snapshot-scoped short ref."""

    ref: str
    tag: str
    role: str | None = None
    type: str | None = None
    text: str
    name: str | None = None
    placeholder: str | None = None
    href: str | None = None
    disabled: bool


class BrowserErrorEvent(BaseModel):
    """Bounded page/console/request failure metadata."""

    page_id: str
    kind: str
    message: str | None = None
    method: str | None = None
    url: str | None = None
    failure: str | None = None


class BrowserSnapshotOutput(BaseModel):
    """Bounded browser snapshot suitable for model-driven interaction."""

    browser_session_id: str
    page_id: str
    title: str
    url: str
    pages: list[BrowserPageSummary]
    text: str | None
    text_truncated: bool
    interactive_elements: list[BrowserInteractiveElement]
    errors: list[BrowserErrorEvent]
    screenshot_path: str | None


class BrowserActionResult(BaseModel):
    """One structured action result."""

    index: int
    action: str
    status: int | None = None
    url: str | None = None
    page_id: str | None = None
    closed: bool | None = None
    waited_ms: int | None = None
    matched: str | None = None
    target: str | None = None


class BrowserActOutput(BaseModel):
    """Result of a bounded ordered browser action sequence."""

    browser_session_id: str
    page_id: str
    title: str
    url: str
    pages: list[BrowserPageSummary]
    results: list[BrowserActionResult]
