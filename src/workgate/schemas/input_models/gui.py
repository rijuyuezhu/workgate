"""Typed input annotations for session-scoped native desktop GUI automation."""

from typing import Annotated, Literal, NotRequired

from pydantic import Field
from typing_extensions import TypedDict

GuiWindowIdArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=1024,
        description="Opaque native window id returned by gui_list.",
    ),
]
GuiStateIdArg = Annotated[
    str,
    Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
        description="Single-use short-lived state_id returned by gui_state.",
    ),
]
GuiScreenshotArg = Annotated[
    bool,
    Field(description="Include a native screenshot in the gui_state result."),
]
GuiIncludeElementsArg = Annotated[
    bool,
    Field(description="Include bounded accessibility elements in gui_state."),
]
GuiMaxElementsArg = Annotated[
    int,
    Field(
        ge=1, le=1000, description="Maximum accessibility elements to return."
    ),
]
GuiMaxDepthArg = Annotated[
    int,
    Field(
        ge=1, le=20, description="Maximum accessibility tree depth to traverse."
    ),
]
GuiElementId = Annotated[
    str,
    Field(
        min_length=1, max_length=1024, description="Element id from gui_state."
    ),
]
GuiCoordinate = Annotated[int, Field(ge=0, le=1_000_000)]
GuiText = Annotated[
    str,
    Field(
        max_length=4096, description="Bounded text for a GUI type/set action."
    ),
]
GuiKeyName = Annotated[str, Field(min_length=1, max_length=64)]
GuiKeys = Annotated[
    Annotated[str, Field(min_length=1, max_length=256)]
    | Annotated[list[GuiKeyName], Field(min_length=1, max_length=16)],
    Field(description="Bounded key chord or ordered key parts."),
]


class GuiPointerAction(TypedDict, closed=True):
    type: Literal["click", "double_click", "right_click", "move"]
    element_id: NotRequired[GuiElementId]
    x: NotRequired[GuiCoordinate]
    y: NotRequired[GuiCoordinate]


class GuiScrollAction(TypedDict, closed=True):
    type: Literal["scroll"]
    element_id: NotRequired[GuiElementId]
    x: NotRequired[GuiCoordinate]
    y: NotRequired[GuiCoordinate]
    amount: NotRequired[Annotated[int, Field(ge=-100, le=100)]]
    delta_x: NotRequired[Annotated[float, Field(ge=-100, le=100)]]
    delta_y: NotRequired[Annotated[float, Field(ge=-100, le=100)]]


class GuiDragAction(TypedDict, closed=True):
    type: Literal["drag"]
    element_id: NotRequired[GuiElementId]
    x: NotRequired[GuiCoordinate]
    y: NotRequired[GuiCoordinate]
    to_x: GuiCoordinate
    to_y: GuiCoordinate


class GuiTypeAction(TypedDict, closed=True):
    type: Literal["type"]
    element_id: NotRequired[GuiElementId]
    text: GuiText


class GuiKeyAction(TypedDict, closed=True):
    type: Literal["key"]
    element_id: NotRequired[GuiElementId]
    keys: GuiKeys


class GuiSetValueAction(TypedDict, closed=True):
    type: Literal["set_value"]
    element_id: GuiElementId
    text: GuiText


class GuiFocusAction(TypedDict, closed=True):
    type: Literal["focus"]
    element_id: NotRequired[GuiElementId]


class GuiWaitAction(TypedDict, closed=True):
    type: Literal["wait"]
    seconds: NotRequired[Annotated[float, Field(ge=0, le=30)]]


GuiAction = Annotated[
    GuiPointerAction
    | GuiScrollAction
    | GuiDragAction
    | GuiTypeAction
    | GuiKeyAction
    | GuiSetValueAction
    | GuiFocusAction
    | GuiWaitAction,
    Field(discriminator="type"),
]
GuiActionsArg = Annotated[
    list[GuiAction],
    Field(
        min_length=1,
        max_length=32,
        description="Actions against one fresh gui_state observation.",
    ),
]
