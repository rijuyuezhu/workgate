"""Owned foreground model-loop checkpoints, adapted from pinned AgentRunner.

Only durable loop state is serialized. Provider credentials, tool definitions
and grants are supplied afresh by the authenticated caller on recovery.
"""

import json
import time
from dataclasses import asdict, dataclass

from .agent_runtime import AgentContext, AgentLoopState, RuntimeSafetyError
from .checkpoint_types import LoopSnapshot
from .checkpoints import SQLiteCheckpointSaver
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .graph_member_scope import GraphMemberScope
from .provider_common import ModelToolCall
from .subagent_scope import (
    DetachedExecutionScope,
    SubagentGraphScope,
    SubagentScope,
)


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
        scope: GraphMemberScope
        | SubagentScope
        | SubagentGraphScope
        | DetachedExecutionScope
        | None = None,
    ):
        if lease.detached_task_id is not None and not isinstance(
            scope, DetachedExecutionScope
        ):
            raise PermissionError(
                "Detached checkpoint requires its admitted execution scope"
            )
        self.lease = lease
        if isinstance(scope, SubagentGraphScope) and scope.member_id is None:
            raise PermissionError("Team scheduler cannot own a member loop")
        self.scope = scope
        self._marker_key = (
            "ownedSubagentLoopCheckpoints"
            if isinstance(
                scope,
                (SubagentScope, SubagentGraphScope, DetachedExecutionScope),
            )
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
        snapshot = LoopSnapshot.model_validate(checkpoint["channel_values"])
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
        snapshot = LoopSnapshot(
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
