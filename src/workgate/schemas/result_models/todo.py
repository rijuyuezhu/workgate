"""Typed structured outputs for todo tools."""

from pydantic import BaseModel, Field


class TodoItem(BaseModel):
    """One persisted agent todo item."""

    id: str = Field(description="Stable todo identifier.")
    content: str = Field(description="Todo text shown to the agent.")
    status: str = Field(
        description="Todo status, such as pending, in_progress, or completed."
    )
    priority: str = Field(
        description="Todo priority label, such as low, medium, or high."
    )


class ReadTodosOutput(BaseModel):
    """Current persisted agent todo list."""

    updated_at: float | None = Field(
        default=None,
        description="Unix timestamp when the todo list was last written, when known.",
    )
    todos: list[TodoItem] = Field(description="Persisted todo items.")


class WriteTodosOutput(ReadTodosOutput):
    """Todo list replacement result."""
