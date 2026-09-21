"""Session-scoped structured browser tool registry."""

from ...schemas.input_models.browser import (
    BrowserActionsArg,
    BrowserFullPageArg,
    BrowserHeadlessArg,
    BrowserIncludeTextArg,
    BrowserMaxElementsArg,
    BrowserMaxTextCharsArg,
    BrowserPageIdArg,
    BrowserScreenshotPathArg,
    BrowserSessionActionArg,
    BrowserSessionIdArg,
    BrowserTimeoutMsArg,
    BrowserUrlArg,
    BrowserViewportHeightArg,
    BrowserViewportWidthArg,
    BrowserWaitUntilArg,
    RequiredBrowserSessionIdArg,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.browser import (
    BrowserActOutput,
    BrowserSessionOutput,
    BrowserSnapshotOutput,
)
from ..declarative import DeclarativeToolRegistry


class BrowserToolRegistry(DeclarativeToolRegistry):
    """Register executor-routed structured browser automation."""

    name = "browser"


browser_tool = BrowserToolRegistry.get_tool_decorator()


@browser_tool(
    http_method="POST",
    http_path="/tools/browser_session",
    oauth_scopes=("browser:use",),
)
async def browser_session(
    session_id: SessionIdArg,
    action: BrowserSessionActionArg,
    browser_session_id: BrowserSessionIdArg = None,
    url: BrowserUrlArg = None,
    headless: BrowserHeadlessArg = True,
    width: BrowserViewportWidthArg = 1440,
    height: BrowserViewportHeightArg = 1000,
    wait_until: BrowserWaitUntilArg = "domcontentloaded",
) -> BrowserSessionOutput:
    """Start, list, or close ephemeral Chromium sessions owned by one Workgate session. Browser resources live on the bound executor, are isolated from other Workgate sessions, and are discarded on executor restart. Only http(s) navigation is permitted. Use browser_snapshot after starting to obtain visible text and stable short element refs."""
    del (
        session_id,
        action,
        browser_session_id,
        url,
        headless,
        width,
        height,
        wait_until,
    )
    raise RuntimeError("browser_session requires control routing")


@browser_tool(
    http_method="POST",
    http_path="/tools/browser_snapshot",
    oauth_scopes=("browser:use",),
)
async def browser_snapshot(
    session_id: SessionIdArg,
    browser_session_id: RequiredBrowserSessionIdArg,
    page_id: BrowserPageIdArg = None,
    include_text: BrowserIncludeTextArg = True,
    max_text_chars: BrowserMaxTextCharsArg = 100_000,
    max_elements: BrowserMaxElementsArg = 100,
    screenshot_path: BrowserScreenshotPathArg = None,
    full_page: BrowserFullPageArg = False,
) -> BrowserSnapshotOutput:
    """Capture bounded page state from a browser owned by the Workgate session. The snapshot returns visible text, recent errors, page metadata, and short refs such as e1 for visible interactive elements. Re-snapshot after navigation or DOM replacement before reusing refs. screenshot_path optionally writes a new PNG inside the session workdir so existing view_image/create_file_link flows can consume it."""
    del (
        session_id,
        browser_session_id,
        page_id,
        include_text,
        max_text_chars,
        max_elements,
        screenshot_path,
        full_page,
    )
    raise RuntimeError("browser_snapshot requires control routing")


@browser_tool(
    http_method="POST",
    http_path="/tools/browser_act",
    oauth_scopes=("browser:use",),
)
async def browser_act(
    session_id: SessionIdArg,
    browser_session_id: RequiredBrowserSessionIdArg,
    actions: BrowserActionsArg,
    page_id: BrowserPageIdArg = None,
    timeout_ms: BrowserTimeoutMsArg = 30_000,
) -> BrowserActOutput:
    """Perform bounded high-level browser actions on an owned browser session. Prefer snapshot refs such as e1 over CSS selectors. Supported actions are navigate, new_page, close_page, click, fill, type, select, press, check, uncheck, hover, wait, wait_for_text, and wait_for_url. There is intentionally no arbitrary Playwright-script escape hatch in this structured surface."""
    del session_id, browser_session_id, actions, page_id, timeout_ms
    raise RuntimeError("browser_act requires control routing")
