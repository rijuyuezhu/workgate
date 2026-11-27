"""Exact member tool edge carried by an official SDK graph interrupt."""

from dataclasses import asdict, dataclass

from .graph_member_scope import GraphMemberScope
from .loop_checkpoint import OwnedLoopCheckpoint
from .provider_common import ModelToolCall, complete_tool_batch


@dataclass(frozen=True)
class GraphHumanRequest:
    scope: GraphMemberScope
    checkpoint_id: str
    calls: list[ModelToolCall]
    question: bool

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint_id, str) or not self.checkpoint_id:
            raise ValueError("Graph human request requires an exact checkpoint")
        if (
            type(self.question) is not bool
            or not self.calls
            or self.question
            and len(self.calls) != 1
        ):
            raise ValueError("Invalid graph human action proposal")
        complete_tool_batch(self.calls)

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: OwnedLoopCheckpoint,
        calls: list[ModelToolCall],
        *,
        question: bool = False,
    ):
        identity = checkpoint.active["configurable"].get("checkpoint_id")
        if checkpoint.scope is None or not isinstance(identity, str):
            raise ValueError(
                "Graph human request requires a saved member tool edge"
            )
        return cls(checkpoint.scope, identity, calls, question)

    def value(self) -> dict:
        return asdict(self)

    @classmethod
    def parse(cls, value: dict):
        if not isinstance(value, dict) or set(value) != {
            "scope",
            "checkpoint_id",
            "calls",
            "question",
        }:
            raise ValueError("Invalid graph human request")
        if (
            not isinstance(value["checkpoint_id"], str)
            or type(value["question"]) is not bool
        ):
            raise ValueError("Invalid graph human request identity")
        return cls(
            GraphMemberScope(**value["scope"]),
            value["checkpoint_id"],
            [ModelToolCall(**call) for call in value["calls"]],
            value["question"],
        )


class GraphHumanActionRequested(Exception):
    def __init__(self, request: GraphHumanRequest):
        self.request = request
        super().__init__("Graph member requires a human action")
