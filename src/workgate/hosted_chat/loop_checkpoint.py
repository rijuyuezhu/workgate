"""Owned foreground model-loop checkpoints, adapted from pinned AgentRunner.

Only durable loop state is serialized. Provider credentials, tool definitions
and grants are supplied afresh by the authenticated caller on recovery.
"""

import time
from dataclasses import asdict, dataclass

from pydantic import BaseModel, ConfigDict, Field

from .agent_runtime import AgentContext, AgentLoopState, RuntimeSafetyError
from .checkpoints import SQLiteCheckpointSaver
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .provider_common import ModelToolCall, complete_tool_batch


class _Snapshot(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    messages: list[dict]
    step: int = Field(ge=0)
    model_turn: int = Field(ge=0)
    agent_id: str | None
    pending_calls: list[dict]
    tool_step_started: bool
    completed: bool
    partial_text: str
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


@dataclass(frozen=True)
class LoopRecovery:
    state: AgentLoopState
    partial_text: str


class OwnedLoopCheckpoint:
    """One authenticated generation actor's ordered event-actor checkpoints."""

    def __init__(self, lease: ActorLease):
        self.lease = lease
        with lease.database.read_snapshot() as db:
            lease.assert_current(db)
            row = db.execute(
                "SELECT conversation_id FROM runs WHERE id=?", (lease.run_id,)
            ).fetchone()
            if row is None:
                raise ActorLeaseLost("Generation no longer exists")
            conversation = row["conversation_id"]
        self.saver = SQLiteCheckpointSaver(
            lease.database, lease.owner, actor_lease=lease
        )
        self.base = self.saver.config(
            conversation, lease.run_id, event_actor=True
        )
        self.active = self.base
        self._recovery_required = self.saver.get_tuple(self.base) is not None

    def restore(self, context: AgentContext) -> LoopRecovery | None:
        self.lease.assert_current()
        row = self.saver.get_tuple(self.base)
        self.lease.assert_current()
        if row is None:
            if self._recovery_required:
                raise RuntimeSafetyError(
                    "Agent loop checkpoint is no longer available"
                )
            return None
        checkpoint = row["checkpoint"]
        if checkpoint.get("v") != 1 or row["metadata"].get("source") != "loop":
            raise ValueError("Invalid Agent loop checkpoint")
        snapshot = _Snapshot.model_validate(checkpoint["channel_values"])
        if snapshot.agent_id != (context.agent or {}).get("id"):
            raise RuntimeSafetyError("Checkpoint Agent is no longer available")
        state = AgentLoopState(
            context=context,
            messages=snapshot.messages,
            step=snapshot.step,
            model_turn=snapshot.model_turn,
            pending_calls=snapshot.calls(),
            tool_step_started=snapshot.tool_step_started,
            completed=snapshot.completed,
            graph_commands=snapshot.graph_commands,
            repeated_tool_batches=snapshot.repeated_tool_batches,
            last_tool_batch=snapshot.last_tool_batch,
        )
        self.active = row["config"]
        self._recovery_required = False
        return LoopRecovery(state, snapshot.partial_text)

    async def save(
        self,
        state: AgentLoopState,
        calls: list[ModelToolCall],
        *,
        partial_text: str = "",
    ) -> None:
        self.lease.assert_current()
        if self._recovery_required:
            raise RuntimeSafetyError(
                "Restore existing Agent loop checkpoint before saving"
            )
        if calls != state.pending_calls:
            raise ValueError(
                "Checkpoint proposal does not match the loop state"
            )
        snapshot = _Snapshot(
            messages=state.messages,
            step=state.step,
            model_turn=state.model_turn,
            agent_id=(state.context.agent or {}).get("id"),
            pending_calls=[asdict(call) for call in calls],
            tool_step_started=state.tool_step_started,
            completed=state.completed,
            partial_text=partial_text,
            graph_commands=state.graph_commands,
            repeated_tool_batches=state.repeated_tool_batches,
            last_tool_batch=state.last_tool_batch,
        )
        snapshot.calls()
        parent = self.active["configurable"].get("checkpoint_id")
        identity = (
            f"{max(time.time_ns(), int(parent) + 1 if parent else 0):020d}"
        )
        self.active = self.saver.put(
            self.active,
            {
                "v": 1,
                "id": identity,
                "channel_values": snapshot.model_dump(),
                "channel_versions": {"messages": state.step},
                "versions_seen": {},
            },
            {"source": "loop", "step": state.step},
        )
