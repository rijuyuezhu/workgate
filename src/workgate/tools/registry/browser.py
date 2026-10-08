"""Session-scoped structured browser tool registry."""

from ...schemas.input_models.browser import (
    BrowserActionsArg,
    BrowserFullPageArg,
    BrowserHeadlessArg,
    BrowserIncludeTextArg,
    BrowserMaxElementsArg,
    BrowserMaxTextCharsArg,
    BrowserPageIdArg,
    BrowserProfileIdArg,
    BrowserScreenshotArg,
    BrowserScreenshotPathArg,
    BrowserSessionActionArg,
    BrowserSessionIdArg,
    BrowserStorageStatePathArg,
    BrowserTimeoutMsArg,
    BrowserUrlArg,
    BrowserViewportHeightArg,
    BrowserViewportWidthArg,
    BrowserWaitUntilArg,
    RequiredBrowserSessionIdArg,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.input_models.shell import (
    PythonCodeArg,
    ShellMaxOutputBytesArg,
    ShellTimeoutArg,
)
from ...schemas.result_models.browser import (
    BrowserActOutput,
    BrowserSessionOutput,
    BrowserSnapshotOutput,
)
from ...schemas.result_models.shell import RunPythonCodeOutput
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
    profile_id: BrowserProfileIdArg = None,
    storage_state_path: BrowserStorageStatePathArg = None,
    save_storage_state_path: BrowserStorageStatePathArg = None,
) -> BrowserSessionOutput:
    """Start, list, or close session-owned Chromium browsers. Ephemeral state ends with the Workgate session; an explicitly saved profile or storage state persists. profile_id and storage_state_path cannot be combined; authentication data is never returned."""
    del (
        session_id,
        action,
        browser_session_id,
        url,
        headless,
        width,
        height,
        wait_until,
        profile_id,
        storage_state_path,
        save_storage_state_path,
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
    screenshot: BrowserScreenshotArg = True,
    screenshot_path: BrowserScreenshotPathArg = None,
    full_page: BrowserFullPageArg = False,
) -> BrowserSnapshotOutput:
    """Capture bounded browser page state and element references. Refresh the snapshot after navigation or DOM changes before acting on refs. By default saves a screenshot; screenshot_path chooses a user-managed PNG."""
    del (
        session_id,
        browser_session_id,
        page_id,
        include_text,
        max_text_chars,
        max_elements,
        screenshot,
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
    """Act on a session-owned browser using fresh snapshot element refs or an unambiguous CSS selector. Use browser_run_script for custom Python."""
    del session_id, browser_session_id, actions, page_id, timeout_ms
    raise RuntimeError("browser_act requires control routing")


@browser_tool(
    http_method="POST",
    http_path="/tools/browser_run_script",
    oauth_scopes=("browser:use", "shell:read", "shell:execute"),
)
async def browser_run_script(
    session_id: SessionIdArg,
    script: PythonCodeArg,
    timeout_s: ShellTimeoutArg = 60,
    max_output_bytes: ShellMaxOutputBytesArg = None,
) -> RunPythonCodeOutput:
    """Run standalone Python Playwright code on the bound executor. This does not attach to browser_session; use browser_act for existing managed browser sessions."""
    del session_id, script, timeout_s, max_output_bytes
    raise RuntimeError("browser_run_script requires control routing")
