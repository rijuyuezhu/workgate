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
    """Observe one native desktop window. Returns bounded accessibility state, an optional native screenshot, and a short-lived single-use state_id. Prefer element ids; coordinate actions are window-relative."""
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
    """Perform bounded native GUI actions against one fresh gui_state observation. The state_id is single-use; semantic targets are preferred over window-relative coordinate fallback. Actions execute sequentially, not transactionally: once execution starts, the state is consumed and earlier side effects are not rolled back if a later action fails, so re-observe after any error."""
    del session_id, window_id, state_id, actions
    raise RuntimeError("gui_action requires control routing")
