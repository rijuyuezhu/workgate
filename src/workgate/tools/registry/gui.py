"""Session-bound native desktop GUI automation tool registry."""

from ...schemas.input_models.gui import (
    GuiActionsArg,
    GuiIncludeElementsArg,
    GuiMaxDepthArg,
    GuiMaxElementsArg,
    GuiScreenshotArg,
    GuiStateIdArg,
    GuiWindowIdArg,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.gui import (
    GuiActionOutput,
    GuiListOutput,
    GuiStateOutput,
)
from ..declarative import DeclarativeToolRegistry


class GuiToolRegistry(DeclarativeToolRegistry):
    """Register executor-routed native desktop automation."""

    name = "gui"


gui_tool = GuiToolRegistry.get_tool_decorator()


@gui_tool(
    http_method=None,
    http_path=None,
    annotations="read_only",
    oauth_scopes=("gui:use",),
)
async def gui_list(session_id: SessionIdArg) -> GuiListOutput:
    """List visible desktop application windows on the executor bound to this Workgate session."""
    del session_id
    raise RuntimeError("gui_list requires control routing")


@gui_tool(
    http_method=None,
    http_path=None,
    oauth_scopes=("gui:use",),
)
async def gui_state(
    session_id: SessionIdArg,
    window_id: GuiWindowIdArg,
    screenshot: GuiScreenshotArg = True,
    include_elements: GuiIncludeElementsArg = True,
    max_elements: GuiMaxElementsArg = 300,
    max_depth: GuiMaxDepthArg = 12,
) -> GuiStateOutput:
    """Observe bounded native window state and optional screenshot. The returned state_id is single-use; prefer accessibility element IDs over window-relative coordinates."""
    del (
        session_id,
        window_id,
        screenshot,
        include_elements,
        max_elements,
        max_depth,
    )
    raise RuntimeError("gui_state requires control routing")


@gui_tool(
    http_method=None,
    http_path=None,
    oauth_scopes=("gui:use",),
    timeout_cancellable=False,
)
async def gui_action(
    session_id: SessionIdArg,
    window_id: GuiWindowIdArg,
    state_id: GuiStateIdArg,
    actions: GuiActionsArg,
) -> GuiActionOutput:
    """Act on a fresh, single-use gui_state. Actions are sequential, not transactional: earlier side effects are not rolled back on error; observe again before retrying."""
    del session_id, window_id, state_id, actions
    raise RuntimeError("gui_action requires control routing")
