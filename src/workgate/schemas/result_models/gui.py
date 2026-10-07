"""Structured native desktop GUI tool outputs."""

from typing import Any

from pydantic import BaseModel, Field


class GuiWindow(BaseModel):
    id: str
    title: str = ""
    app: str = ""
    pid: int = 0
    bounds: dict[str, int] = Field(default_factory=dict)


class GuiListOutput(BaseModel):
    session_id: str
    backend: str
    windows: list[GuiWindow] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class GuiScreenshotMetadata(BaseModel):
    mime_type: str
    bytes: int = Field(ge=1)


class GuiStateOutput(BaseModel):
    session_id: str
    backend: str
    state_id: str
    state_ttl_s: float = Field(gt=0)
    window: dict[str, Any]
    elements: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    screenshot: GuiScreenshotMetadata | None = None


class GuiActionOutput(BaseModel):
    session_id: str
    backend: str
    state_id: str
    window_id: str
    state_consumed: bool
    actions: list[dict[str, Any]] = Field(default_factory=list)
