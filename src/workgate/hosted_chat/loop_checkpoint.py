"""Owned foreground model-loop checkpoints, adapted from pinned AgentRunner.

Only durable loop state is serialized. Provider credentials, tool definitions
and grants are supplied afresh by the authenticated caller on recovery.
"""

import json
import time
from dataclasses import asdict, dataclass

from pydantic import BaseModel, ConfigDict, Field

from .agent_runtime import AgentContext, AgentLoopState, RuntimeSafetyError
from .checkpoints import SQLiteCheckpointSaver
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .graph_member_scope import GraphMemberScope
from .provider_common import ModelToolCall, complete_tool_batch
from .subagent_scope import SubagentScope


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


@dataclass(frozen=True)
class LoopRecovery:
    state: AgentLoopState
    partial_text: str
    public_delta_cursor: int


class OwnedLoopCheckpoint:
    """One authenticated generation actor's ordered event-actor checkpoints."""

    def __init__(
        self,
        lease: ActorLease,
        *,
        scope: GraphMemberScope | SubagentScope | None = None,
    ):
        self.lease = lease
        self.scope = scope
        self._marker_key = (
            "ownedSubagentLoopCheckpoints"
            if isinstance(scope, SubagentScope)
            else "ownedGraphMemberCheckpoints"
        )
        with lease.database.read_snapshot() as db:
            lease.assert_current(db)
            row = db.execute(
                "SELECT conversation_id,state FROM runs WHERE id=?",
                (lease.run_id,),
            ).fetchone()
            if row is None:
                raise ActorLeaseLost("Generation no longer exists")
            conversation = row["conversation_id"]
            run_state = json.loads(row["state"])
            if scope is None:
                started = run_state.get("ownedLoopCheckpoint") is True
            else:
                scope.resolve(db, lease)
                marker = run_state.get(self._marker_key, {}).get(
                    scope.checkpoint_namespace
                )
                if marker is not None and marker != scope.marker:
                    raise RuntimeSafetyError(
                        "Graph activation checkpoint identity changed"
                    )
                started = marker is not None
        self.saver = SQLiteCheckpointSaver(
            lease.database, lease.owner, actor_lease=lease, member_scope=scope
        )
        self.base = self.saver.config(
            conversation,
            scope.checkpoint_namespace if scope is not None else lease.run_id,
            event_actor=True,
        )
        self.active = self.base
        self._recovery_required = (
            started or self.saver.get_tuple(self.base) is not None
        )
        self._started = started

    def assert_current(self) -> None:
        if self.scope is None:
            self.lease.assert_current()
        else:
            with self.lease.database.read_snapshot() as db:
                self.scope.resolve(db, self.lease)

    def _check_context(self, context: AgentContext) -> None:
        if (
            self.scope is not None
            and (context.agent or {}).get("id") != self.scope.member_id
        ):
            raise RuntimeSafetyError(
                "Graph member context does not match its activation"
            )

    def restore(self, context: AgentContext) -> LoopRecovery | None:
        self.assert_current()
        self._check_context(context)
        row = self.saver.get_tuple(self.base)
        self.assert_current()
        if row is None:
            if self._recovery_required or self._started:
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
        return LoopRecovery(
            state, snapshot.partial_text, snapshot.public_delta_cursor
        )

    async def save(
        self,
        state: AgentLoopState,
        calls: list[ModelToolCall],
        *,
        partial_text: str = "",
        public_delta_cursor: int = 0,
    ) -> None:
        self.assert_current()
        self._check_context(state.context)
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
            public_delta_cursor=public_delta_cursor,
            graph_commands=state.graph_commands,
            repeated_tool_batches=state.repeated_tool_batches,
            last_tool_batch=state.last_tool_batch,
        )
        snapshot.calls()
        if not self._started:
            # Record the recovery obligation before storing the first edge.
            # Expiry/pruning must not turn a previously started run into a
            # fresh model turn with potentially new native effect identities.
            with self.lease.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.lease.assert_current(db)
                if self.scope is None:
                    db.execute(
                        "UPDATE runs SET state=json_set(state,'$.ownedLoopCheckpoint',json('true')) WHERE id=?",
                        (self.lease.run_id,),
                    )
                else:
                    self.scope.resolve(db, self.lease)
                    row = db.execute(
                        "SELECT state FROM runs WHERE id=?",
                        (self.lease.run_id,),
                    ).fetchone()
                    run_state = json.loads(row["state"])
                    markers = run_state.setdefault(self._marker_key, {})
                    if self.scope.checkpoint_namespace in markers:
                        raise RuntimeSafetyError(
                            "Graph activation already has a recovery obligation"
                        )
                    markers[self.scope.checkpoint_namespace] = self.scope.marker
                    db.execute(
                        "UPDATE runs SET state=? WHERE id=?",
                        (
                            json.dumps(run_state, separators=(",", ":")),
                            self.lease.run_id,
                        ),
                    )
            self._started = True
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
