"""Foreground human-action interruptions, adapted from pinned generations.py.

The approval claim and its exact started-edge checkpoint share the same
SQLite transaction. A resolution is review evidence, never a native grant.
"""

import json
import sqlite3
import time
import uuid
from collections.abc import Collection, Mapping

from .approval_runtime import (
    ApprovalBatch,
    build_approval_payload,
    prepare_approval_batch,
)
from .ask_user_tools import (
    ASK_USER_QUESTION_TOOL_NAME,
    build_ask_user_payload,
    resolve_ask_user_question_resume,
)
from .checkpoint_types import LoopSnapshot
from .checkpoints import SQLiteCheckpointSaver
from .content_filters import (
    filters_from_snapshot,
    validate_message_submission,
    validate_tool_arguments,
)
from .database import SQLiteDatabase
from .durable_tasks import DurableTaskRepository
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .generation_protocol import EVENT_LIMIT, MAX_EVENT_BYTES
from .generations import GenerationStore, _json
from .graph_human_request import GraphHumanRequest
from .graph_member_scope import GraphMemberScope
from .loop_checkpoint import OwnedLoopCheckpoint
from .provider_common import ModelToolCall
from .storage import _now


def _claim_state(state: dict, namespace: str, *, create: bool = False) -> dict:
    if not namespace:
        return state
    if create:
        return state.setdefault("graphHumanClaims", {}).setdefault(
            namespace, {}
        )
    return state.get("graphHumanClaims", {}).get(namespace, {})


def _namespace(checkpoint: OwnedLoopCheckpoint) -> str:
    return (
        checkpoint.scope.checkpoint_namespace
        if checkpoint.scope is not None
        else ""
    )


class ApprovalClaimStore:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def _row(
        self, db: sqlite3.Connection, owner: str, run_id: str, status: str
    ):
        row = db.execute(
            "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id "
            "WHERE r.id=? AND c.owner=? AND r.status=?",
            (run_id, owner, status),
        ).fetchone()
        if row is None:
            raise KeyError("No owned generation action")
        return row

    @staticmethod
    def _edge(
        db: sqlite3.Connection,
        saver: SQLiteCheckpointSaver,
        base: dict,
        identity: str,
    ) -> dict:
        row = saver._get_tuple(db, base)
        if (
            row is None
            or row["config"]["configurable"]["checkpoint_id"] != identity
            or row["metadata"].get("source") != "loop"
        ):
            raise ValueError("Approval checkpoint is missing or stale")
        snapshot = LoopSnapshot.model_validate(
            row["checkpoint"]["channel_values"]
        )
        if not snapshot.tool_step_started or snapshot.completed:
            raise ValueError("Approval requires a started tool edge")
        snapshot.calls()
        return row

    @staticmethod
    def _verify_requests(
        edge: dict, payload: dict, *, allow_settled: bool = False
    ) -> None:
        snapshot = LoopSnapshot.model_validate(
            edge["checkpoint"]["channel_values"]
        )
        saved = {call.call_id: call for call in snapshot.calls()}
        settled = {
            task_id
            for task_id, channel, _ in edge["pendingWrites"]
            if channel == "tool_receipt"
        }
        for request in payload["action_requests"]:
            identity = request["tool_call_id"]
            call = saved.get(identity)
            if (
                call is None
                or call.name != request["name"]
                or json.dumps(call.arguments, sort_keys=True, allow_nan=False)
                != json.dumps(
                    request["arguments"], sort_keys=True, allow_nan=False
                )
                or (not allow_settled and f"receipt:{identity}" in settled)
            ):
                raise ValueError(
                    "Approval proposal is not an unsettled saved tool call"
                )

    def _question_payload(
        self,
        edge: dict,
        state: dict,
        identity: str,
        call_id: str,
        *,
        allow_settled: bool = False,
    ) -> dict:
        calls = LoopSnapshot.model_validate(
            edge["checkpoint"]["channel_values"]
        ).calls()
        if (
            len(calls) != 1
            or calls[0].name != ASK_USER_QUESTION_TOOL_NAME
            or calls[0].call_id != call_id
        ):
            raise ValueError("Ask User requires one exact saved question call")
        original = calls[0]
        self._verify_requests(
            edge,
            build_approval_payload([original]),
            allow_settled=allow_settled,
        )
        effective = original
        claim = state.get("approvedToolBatch")
        if claim is not None and claim["checkpointId"] == identity:
            self._verify_requests(
                edge, claim["payload"], allow_settled=allow_settled
            )
            batch = prepare_approval_batch(
                claim["payload"], claim["resolutions"]
            )
            [entry] = batch.entries
            if entry.call_id != call_id or entry.decision["type"] not in {
                "approve",
                "edit",
            }:
                raise ValueError("Ask User review did not authorize a question")
            effective = entry.tool_call()
        return build_ask_user_payload(effective.arguments, call_id)

    def pause(
        self,
        checkpoint: OwnedLoopCheckpoint,
        calls: list[ModelToolCall],
        *,
        exact_tools: Collection[str] = (),
        descriptions: Mapping[str, str] | None = None,
    ) -> dict:
        return self._pause(
            checkpoint,
            calls,
            question=False,
            exact_tools=exact_tools,
            descriptions=descriptions,
        )

    def pause_question(
        self, checkpoint: OwnedLoopCheckpoint, call: ModelToolCall
    ) -> dict:
        return self._pause(checkpoint, [call], question=True)

    def _pause(
        self,
        checkpoint: OwnedLoopCheckpoint,
        calls: list[ModelToolCall],
        *,
        question: bool,
        graph_interrupt: tuple[str, GraphHumanRequest] | None = None,
        exact_tools: Collection[str] = (),
        descriptions: Mapping[str, str] | None = None,
    ) -> dict:
        if checkpoint.scope is not None and graph_interrupt is None:
            raise PermissionError(
                "Graph member human actions require graph-aware recovery"
            )
        lease = checkpoint.lease
        if lease.detached_task_id is not None:
            raise PermissionError("Detached child cannot request human actions")
        if lease.database.path != self.database.path:
            raise ActorLeaseLost(
                "Approval checkpoint belongs to another database"
            )
        payload = build_approval_payload(
            calls,
            decisions_by_tool_name={
                "set_memory": ("approve", "reject"),
                "delete_memory": ("approve", "reject"),
                **{name: ("approve", "reject") for name in exact_tools},
            },
            descriptions=descriptions,
        )
        identity = checkpoint.active["configurable"].get("checkpoint_id")
        if not isinstance(identity, str):
            raise ValueError("Approval requires a committed checkpoint")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease.assert_current(db)
            row = self._row(db, lease.owner, lease.run_id, "running")
            state = json.loads(row["state"])
            if checkpoint.scope is not None:
                checkpoint.scope.resolve(db, lease)
            namespace = _namespace(checkpoint)
            claims = _claim_state(state, namespace, create=True)
            edge = self._edge(db, checkpoint.saver, checkpoint.base, identity)
            self._verify_requests(edge, payload)
            if (
                claims.get("answeredQuestion", {}).get("checkpointId")
                == identity
            ):
                raise ValueError(
                    "Question for this edge has already been answered"
                )
            if question:
                payload = self._question_payload(
                    edge, claims, identity, calls[0].call_id
                )
            elif (
                claims.get("approvedToolBatch", {}).get("checkpointId")
                == identity
            ):
                raise ValueError(
                    "Approval for this edge has already been claimed"
                )
            if any(
                call.name in {"set_memory", "delete_memory"} for call in calls
            ):
                state["memoryManualReview"] = True
            pending = {
                "actionId": "action_" + uuid.uuid4().hex,
                "streamId": row["conversation_id"],
                "conversationId": row["conversation_id"],
                "runId": lease.run_id,
                "responseMessageId": state["responseMessageId"],
                "createdAt": int(time.time() * 1000),
                "namespace": namespace,
                "payload": payload,
            }
            event = _json({"event": "on_pending_action", "data": pending})
            if len(event.encode("utf-8")) > MAX_EVENT_BYTES:
                raise ValueError(
                    "Approval event exceeds generation event limit"
                )
            if (
                db.execute(
                    "SELECT count(*) FROM generation_events WHERE run_id=?",
                    (lease.run_id,),
                ).fetchone()[0]
                >= EVENT_LIMIT
            ):
                raise ValueError("Generation event retention limit reached")
            checkpoint.saver._put_writes(
                db,
                checkpoint.active,
                [
                    (
                        "__interrupt__",
                        {"id": pending["actionId"], "value": pending},
                    )
                ],
                "human",
            )
            state["pendingAction"] = pending
            state["approvalCheckpointId"] = identity
            if graph_interrupt is not None:
                from .graph_checkpoint import verify_graph_interrupt

                interrupt_id, request = graph_interrupt
                verify_graph_interrupt(
                    db,
                    checkpoint.saver,
                    row["conversation_id"],
                    request.scope.graph_namespace,
                    request.scope.activation_id,
                    interrupt_id,
                    request.value(),
                )
                state["approvalGraphInterrupt"] = {
                    "id": interrupt_id,
                    "request": request.value(),
                }
            if (
                not question
                or claims.get("approvedToolBatch", {}).get("checkpointId")
                != identity
            ):
                claims.pop("approvedToolBatch", None)
            claims.pop("answeredQuestion", None)
            db.execute(
                "UPDATE runs SET state=?,status='requires_action',updated_at=? WHERE id=?",
                (_json(state), _now(), lease.run_id),
            )
            db.execute(
                "DELETE FROM generation_actor_leases WHERE run_id=?",
                (lease.run_id,),
            )
            db.execute(
                "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
                (lease.run_id, event),
            )
        return pending

    def publish_graph_interrupt(
        self, lease: ActorLease, interrupt_id: str, request: GraphHumanRequest
    ) -> dict:
        """Called only after the SDK has persisted its interrupted superstep."""
        if not isinstance(interrupt_id, str) or not interrupt_id:
            raise ValueError("Graph interrupt identity is required")
        checkpoint = OwnedLoopCheckpoint(lease, scope=request.scope)
        checkpoint.active = checkpoint.saver.config(
            checkpoint.base["configurable"]["thread_id"],
            request.scope.checkpoint_namespace,
            checkpoint_id=request.checkpoint_id,
            event_actor=True,
        )
        return self._pause(
            checkpoint,
            request.calls,
            question=request.question,
            graph_interrupt=(interrupt_id, request),
        )

    def resolved_graph_interrupts(
        self, lease: ActorLease, scope: GraphMemberScope
    ) -> list[dict]:
        with self.database.read_snapshot() as db:
            scope.resolve(db, lease)
            row = self._row(db, lease.owner, lease.run_id, "running")
            claims = _claim_state(
                json.loads(row["state"]), scope.checkpoint_namespace
            )
            return claims.get("resolvedInterrupts", [])

    @staticmethod
    def _record_graph_resolution(
        state: dict, claims: dict, pending: dict
    ) -> None:
        graph = state.pop("approvalGraphInterrupt", None)
        if graph is not None:
            claims.setdefault("resolvedInterrupts", []).append(
                {
                    **graph,
                    "actionId": pending["actionId"],
                }
            )

    def pending(self, owner: str, run_id: str) -> dict | None:
        with self.database.read_snapshot() as db:
            try:
                row = self._row(db, owner, run_id, "requires_action")
            except KeyError:
                return None
            return json.loads(row["state"]).get("pendingAction")

    @staticmethod
    def _expired(pending: dict) -> bool:
        expires = pending.get("expiresAt")
        return (
            isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and expires <= time.time() * 1000
        )

    def _pending_edge(
        self, db, saver: SQLiteCheckpointSaver, row, state: dict, pending: dict
    ) -> dict:
        namespace = pending["namespace"]
        claims = _claim_state(state, namespace)
        request = None
        if namespace:
            graph = state.get("approvalGraphInterrupt")
            if not isinstance(graph, dict):
                raise ValueError("Graph approval has no matching SDK interrupt")
            request = GraphHumanRequest.parse(graph["request"])
            if (
                request.scope.checkpoint_namespace != namespace
                or request.checkpoint_id != state["approvalCheckpointId"]
            ):
                raise ValueError("Graph approval checkpoint identity changed")
            request.scope.resolve_owned(db, saver.owner, row["id"])
            from .graph_checkpoint import verify_graph_interrupt

            verify_graph_interrupt(
                db,
                saver,
                row["conversation_id"],
                request.scope.graph_namespace,
                request.scope.activation_id,
                graph["id"],
                request.value(),
            )
        edge = self._edge(
            db,
            saver,
            saver.config(
                row["conversation_id"], namespace or row["id"], event_actor=True
            ),
            state["approvalCheckpointId"],
        )
        marker = {"id": pending["actionId"], "value": pending}
        if ("human", "__interrupt__", marker) not in edge["pendingWrites"]:
            raise ValueError("Approval has no matching durable interruption")
        payload = pending["payload"]
        if request is not None:
            if request.question != (
                payload.get("type") == ASK_USER_QUESTION_TOOL_NAME
            ):
                raise ValueError(
                    "Graph human action kind differs from SDK request"
                )
            if not request.question and _json(
                build_approval_payload(request.calls)
            ) != _json(payload):
                raise ValueError("Graph approval differs from SDK proposal")
        if payload.get("type") == "tool_approval":
            self._verify_requests(edge, payload)
        elif payload.get("type") == ASK_USER_QUESTION_TOOL_NAME:
            expected = self._question_payload(
                edge,
                claims,
                state["approvalCheckpointId"],
                payload["tool_call_id"],
            )
            if _json(expected) != _json(payload):
                raise ValueError("Question differs from saved proposal")
        else:
            raise ValueError("Unknown pending human action")
        return edge

    def abort_pending(self, owner: str, run_id: str, action_id: str) -> bool:
        """Explicit owner abort also latches cancellation of this run's native work."""
        return self._finish_pending(
            owner, run_id, action_id, expired_only=False
        )

    def expire_pending(self, owner: str, run_id: str, action_id: str) -> bool:
        """Interrupt only an unavailable review; no new authority or native effects."""
        return self._finish_pending(owner, run_id, action_id, expired_only=True)

    def _finish_pending(
        self, owner: str, run_id: str, action_id: str, *, expired_only: bool
    ) -> bool:
        saver = SQLiteCheckpointSaver(self.database, owner)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(db, owner, run_id, "requires_action")
            except KeyError:
                return False
            state = json.loads(row["state"])
            pending = state.get("pendingAction")
            if (
                not isinstance(pending, dict)
                or pending.get("actionId") != action_id
            ):
                return False
            if expired_only:
                if not self._expired(pending):
                    try:
                        self._pending_edge(db, saver, row, state, pending)
                    except ValueError, KeyError, PermissionError:
                        pass
                    else:
                        return False
                status = "interrupted"
                error = "Pending human action expired or its checkpoint is no longer available"
            else:
                DurableTaskRepository._cancel_run(db, owner, run_id)
                status = "aborted"
                error = None
            return self._finish_action_locked(
                db, row, state, status=status, error=error
            )

    def _finish_action_locked(
        self, db, row, state, *, status, error=None
    ) -> bool:
        for key in (
            "pendingAction",
            "approvalCheckpointId",
            "approvedToolBatch",
            "answeredQuestion",
            "approvalGraphInterrupt",
            "graphHumanClaims",
        ):
            state.pop(key, None)
        db.execute(
            "UPDATE runs SET state=? WHERE id=?", (_json(state), row["id"])
        )
        db.execute(
            "DELETE FROM generation_actor_leases WHERE run_id=?", (row["id"],)
        )
        return GenerationStore(self.database)._finish_locked(
            db, row, status=status, error=error
        )

    def abort_root(self, owner: str, run_id: str) -> bool:
        """Latch exact-root native cancellation and terminal state atomically."""
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
                (run_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError("Generation not found")
            state = json.loads(row["state"])
            if (
                not isinstance(state.get("nativeAdmission"), dict)
                or state.get("detachedTaskId")
                or state.get("completionWakeupTaskId")
            ):
                raise PermissionError("Not a public root generation")
            if row["status"] not in {"running", "requires_action"}:
                return False
            DurableTaskRepository._cancel_run(db, owner, run_id)
            # Detached tasks have their own explicit cancellation controls.
            # An aborted parent cannot receive an automatic completion wakeup.
            db.execute(
                "UPDATE subagent_wakeups SET status='retired',error='Original parent aborted',updated_at=? WHERE owner=? AND parent_run_id=? AND status='pending'",
                (_now(), owner, run_id),
            )
            return self._finish_action_locked(db, row, state, status="aborted")

    def claim(
        self, owner: str, run_id: str, action_id: str, resolutions: list[dict]
    ) -> ApprovalBatch:
        saver = SQLiteCheckpointSaver(self.database, owner)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, owner, run_id, "requires_action")
            state = json.loads(row["state"])
            pending = state.get("pendingAction")
            if (
                not isinstance(pending, dict)
                or pending.get("actionId") != action_id
            ):
                raise ValueError("Stale tool approval action")
            if self._expired(pending):
                raise ValueError("Human action expired")
            identity = state["approvalCheckpointId"]
            self._pending_edge(db, saver, row, state, pending)
            payload = pending["payload"]
            if payload.get("type") != "tool_approval":
                raise ValueError("Expected tool approval")
            batch = prepare_approval_batch(payload, resolutions)
            # Persist only source resolution fields; caller metadata is not replay evidence.
            clean = []
            filters = filters_from_snapshot(db, owner)
            for entry in batch.entries:
                decision = entry.decision
                kind = decision["type"]
                item = {"tool_call_id": entry.call_id, "decision": kind}
                if kind == "edit":
                    validate_tool_arguments(
                        entry.name, decision["updatedInput"], filters
                    )
                    item["editedArguments"] = decision["updatedInput"]
                elif kind == "reject" and "reason" in decision:
                    validate_message_submission(decision["reason"], filters)
                    item["reason"] = decision["reason"]
                elif kind == "respond":
                    validate_message_submission(
                        decision["responseText"], filters
                    )
                    item["responseText"] = decision["responseText"]
                clean.append(item)
            claims = _claim_state(state, pending["namespace"], create=True)
            claims["approvedToolBatch"] = {
                "actionId": action_id,
                "checkpointId": identity,
                "payload": payload,
                "resolutions": clean,
            }
            self._record_graph_resolution(state, claims, pending)
            state.pop("pendingAction")
            state.pop("approvalCheckpointId")
            db.execute(
                "UPDATE runs SET state=?,status='running',updated_at=? WHERE id=?",
                (_json(state), _now(), run_id),
            )
        return batch

    def approved_batch(
        self, checkpoint: OwnedLoopCheckpoint
    ) -> ApprovalBatch | None:
        lease = checkpoint.lease
        if lease.database.path != self.database.path:
            raise ActorLeaseLost(
                "Approval checkpoint belongs to another database"
            )
        with self.database.read_snapshot() as db:
            lease.assert_current(db)
            row = self._row(db, lease.owner, lease.run_id, "running")
            if checkpoint.scope is not None:
                checkpoint.scope.resolve(db, lease)
            claims = _claim_state(
                json.loads(row["state"]), _namespace(checkpoint)
            )
            claim = claims.get("approvedToolBatch")
            identity = checkpoint.active["configurable"].get("checkpoint_id")
            if claim is None:
                return None
            if not isinstance(identity, str):
                raise ValueError(
                    "Restore the claimed approval checkpoint before replay"
                )
            edge = self._edge(db, checkpoint.saver, checkpoint.base, identity)
            if claim["checkpointId"] != identity:
                return None
            self._verify_requests(edge, claim["payload"], allow_settled=True)
            return prepare_approval_batch(
                claim["payload"], claim["resolutions"]
            )

    def claim_question(
        self,
        owner: str,
        run_id: str,
        action_id: str,
        body: dict,
    ) -> dict:
        if not isinstance(body, dict):
            raise ValueError("An answer is required")
        saver = SQLiteCheckpointSaver(self.database, owner)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, owner, run_id, "requires_action")
            state = json.loads(row["state"])
            pending = state.get("pendingAction")
            if (
                not isinstance(pending, dict)
                or pending.get("actionId") != action_id
            ):
                raise ValueError("Stale Ask User action")
            if self._expired(pending):
                raise ValueError("Human action expired")
            self._pending_edge(db, saver, row, state, pending)
            payload = pending["payload"]
            if payload.get("type") != ASK_USER_QUESTION_TOOL_NAME:
                raise ValueError("Expected Ask User question")
            resolution = resolve_ask_user_question_resume(payload, body)
            if "error" in resolution:
                raise ValueError(resolution["error"])
            answer = resolution["resumeValue"]
            filters = filters_from_snapshot(db, owner)
            texts = (
                answer["answers"].values()
                if "answers" in answer
                else [answer["answer"]]
            )
            for text in texts:
                validate_message_submission(text, filters)
            claims = _claim_state(state, pending["namespace"], create=True)
            claims["answeredQuestion"] = {
                "actionId": action_id,
                "checkpointId": state["approvalCheckpointId"],
                "payload": payload,
                "answer": answer,
            }
            self._record_graph_resolution(state, claims, pending)
            state.pop("pendingAction")
            state.pop("approvalCheckpointId")
            db.execute(
                "UPDATE runs SET state=?,status='running',updated_at=? WHERE id=?",
                (_json(state), _now(), run_id),
            )
            return answer

    def answered_question(self, checkpoint: OwnedLoopCheckpoint) -> dict | None:
        lease = checkpoint.lease
        if lease.database.path != self.database.path:
            raise ActorLeaseLost(
                "Question checkpoint belongs to another database"
            )
        with self.database.read_snapshot() as db:
            lease.assert_current(db)
            row = self._row(db, lease.owner, lease.run_id, "running")
            if checkpoint.scope is not None:
                checkpoint.scope.resolve(db, lease)
            claims = _claim_state(
                json.loads(row["state"]), _namespace(checkpoint)
            )
            claim = claims.get("answeredQuestion")
            if claim is None:
                return None
            identity = checkpoint.active["configurable"].get("checkpoint_id")
            if not isinstance(identity, str):
                raise ValueError(
                    "Restore the answered question checkpoint before replay"
                )
            edge = self._edge(db, checkpoint.saver, checkpoint.base, identity)
            if claim["checkpointId"] != identity:
                return None
            payload = self._question_payload(
                edge,
                claims,
                identity,
                claim["payload"]["tool_call_id"],
                allow_settled=True,
            )
            if _json(payload) != _json(claim["payload"]):
                raise ValueError(
                    "Answered question differs from saved proposal"
                )
            resolution = resolve_ask_user_question_resume(
                payload, claim["answer"]
            )
            if "error" in resolution:
                raise ValueError(resolution["error"])
            return resolution["resumeValue"]
