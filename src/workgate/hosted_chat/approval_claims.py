"""Foreground approval interruptions, adapted from pinned generations.py.

The approval claim and its exact started-edge checkpoint share the same
SQLite transaction. A resolution is review evidence, never a native grant.
"""

import json
import sqlite3
import time
import uuid

from .approval_runtime import (
    ApprovalBatch,
    build_approval_payload,
    prepare_approval_batch,
)
from .checkpoints import SQLiteCheckpointSaver
from .database import SQLiteDatabase
from .durable_tasks import DurableTaskRepository
from .generation_actor_lease import ActorLeaseLost
from .generations import _EVENT_LIMIT, _MAX_EVENT_BYTES, GenerationStore, _json
from .loop_checkpoint import OwnedLoopCheckpoint, _Snapshot
from .provider_common import ModelToolCall
from .storage import _now


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
        snapshot = _Snapshot.model_validate(row["checkpoint"]["channel_values"])
        if not snapshot.tool_step_started or snapshot.completed:
            raise ValueError("Approval requires a started tool edge")
        snapshot.calls()
        return row

    @staticmethod
    def _verify_requests(
        edge: dict, payload: dict, *, allow_settled: bool = False
    ) -> None:
        snapshot = _Snapshot.model_validate(
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

    def pause(
        self, checkpoint: OwnedLoopCheckpoint, calls: list[ModelToolCall]
    ) -> dict:
        lease = checkpoint.lease
        if lease.database.path != self.database.path:
            raise ActorLeaseLost(
                "Approval checkpoint belongs to another database"
            )
        payload = build_approval_payload(calls)
        identity = checkpoint.active["configurable"].get("checkpoint_id")
        if not isinstance(identity, str):
            raise ValueError("Approval requires a committed checkpoint")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease.assert_current(db)
            row = self._row(db, lease.owner, lease.run_id, "running")
            state = json.loads(row["state"])
            edge = self._edge(db, checkpoint.saver, checkpoint.base, identity)
            self._verify_requests(edge, payload)
            if (
                state.get("approvedToolBatch", {}).get("checkpointId")
                == identity
            ):
                raise ValueError(
                    "Approval for this edge has already been claimed"
                )
            pending = {
                "actionId": "action_" + uuid.uuid4().hex,
                "streamId": row["conversation_id"],
                "conversationId": row["conversation_id"],
                "runId": lease.run_id,
                "responseMessageId": state["responseMessageId"],
                "createdAt": int(time.time() * 1000),
                "namespace": "",
                "payload": payload,
            }
            event = _json({"event": "on_pending_action", "data": pending})
            if len(event.encode("utf-8")) > _MAX_EVENT_BYTES:
                raise ValueError(
                    "Approval event exceeds generation event limit"
                )
            if (
                db.execute(
                    "SELECT count(*) FROM generation_events WHERE run_id=?",
                    (lease.run_id,),
                ).fetchone()[0]
                >= _EVENT_LIMIT
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
            state.pop("approvedToolBatch", None)
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
        edge = self._edge(
            db,
            saver,
            saver.config(row["conversation_id"], row["id"], event_actor=True),
            state["approvalCheckpointId"],
        )
        marker = {"id": pending["actionId"], "value": pending}
        if ("human", "__interrupt__", marker) not in edge["pendingWrites"]:
            raise ValueError("Approval has no matching durable interruption")
        self._verify_requests(edge, pending["payload"])
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
                    except ValueError, KeyError:
                        pass
                    else:
                        return False
                status = "interrupted"
                error = "Pending tool approval expired or its checkpoint is no longer available"
            else:
                DurableTaskRepository._cancel_run(db, owner, run_id)
                status = "aborted"
                error = None
            for key in (
                "pendingAction",
                "approvalCheckpointId",
                "approvedToolBatch",
            ):
                state.pop(key, None)
            db.execute(
                "UPDATE runs SET state=? WHERE id=?", (_json(state), run_id)
            )
            db.execute(
                "DELETE FROM generation_actor_leases WHERE run_id=?", (run_id,)
            )
            return GenerationStore(self.database)._finish_locked(
                db, row, status=status, error=error
            )

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
            batch = prepare_approval_batch(payload, resolutions)
            # Persist only source resolution fields; caller metadata is not replay evidence.
            clean = []
            for entry in batch.entries:
                decision = entry.decision
                kind = decision["type"]
                item = {"tool_call_id": entry.call_id, "decision": kind}
                if kind == "edit":
                    item["editedArguments"] = decision["updatedInput"]
                elif kind == "reject" and "reason" in decision:
                    item["reason"] = decision["reason"]
                elif kind == "respond":
                    item["responseText"] = decision["responseText"]
                clean.append(item)
            state["approvedToolBatch"] = {
                "actionId": action_id,
                "checkpointId": identity,
                "payload": payload,
                "resolutions": clean,
            }
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
            claim = json.loads(row["state"]).get("approvedToolBatch")
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
