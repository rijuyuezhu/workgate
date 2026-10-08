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
    """Start, list, or close Chromium sessions owned by one Workgate session. Ordinary sessions are ephemeral. action=start may explicitly reuse one executor-local durable profile_id or import Playwright storage_state_path; those two start modes are mutually exclusive. action=close may explicitly export storage state to save_storage_state_path. Storage-state paths are relative to the owning Workgate session workdir, and auth payloads are never returned. Live browser resources remain session-owned and are closed by session_end; explicitly persisted auth state is not deleted."""
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
    screenshot_path: BrowserScreenshotPathArg = None,
    full_page: BrowserFullPageArg = False,
) -> BrowserSnapshotOutput:
    """Capture bounded page state from a browser owned by the Workgate session. The snapshot returns visible text, recent errors, page metadata, and short refs such as e1 for visible interactive elements. Re-snapshot after navigation or DOM replacement before reusing refs. screenshot_path optionally writes a new PNG; relative paths resolve from the session workdir."""
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
    """Perform bounded high-level browser actions on an owned browser session. Use fresh snapshot refs for element targets when possible, or a bounded CSS selector matching exactly one element. For arbitrary Python code, use the separate browser_run_script tool with shell execution scope."""
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
    """Run a complete Python Playwright script in a separate Chromium-capable subprocess on the bound executor. Uses the Workgate session workdir and runtime Python (configured Python for frozen executables); the caller must import and launch Playwright. Reuses bounded shell timeout, output and process cleanup; does not attach to an existing browser_session. Prefer browser_act for structured operations."""
    del session_id, script, timeout_s, max_output_bytes
    raise RuntimeError("browser_run_script requires control routing")
