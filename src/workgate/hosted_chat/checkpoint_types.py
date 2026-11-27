"""Shared checkpoint wire schema and namespace identity."""

from pydantic import BaseModel, ConfigDict, Field

from .provider_common import ModelToolCall, complete_tool_batch

NAMESPACE_KEY = "__librechat_checkpoint_ns"


class LoopSnapshot(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    messages: list[dict]
    step: int = Field(ge=0)
    model_turn: int = Field(ge=0)
    agent_id: str | None
    pending_calls: list[dict]
    tool_step_started: bool
    completed: bool
    partial_text: str
    public_delta_cursor: int = Field(default=0, ge=0)
    graph_commands: list[dict]
    repeated_tool_batches: int = Field(ge=0)
    last_tool_batch: str | None

    def calls(self) -> list[ModelToolCall]:
        calls = complete_tool_batch(
            [ModelToolCall(**value) for value in self.pending_calls]
        )
        if (
            self.model_turn > self.step
            or (self.tool_step_started and not calls)
            or (self.completed and calls)
        ):
            raise ValueError("Inconsistent Agent loop checkpoint boundary")
        if self.last_tool_batch is None and self.repeated_tool_batches:
            raise ValueError("Inconsistent Agent loop checkpoint progress")
        return calls
