"""Root foreground Actions use the existing durable finite-effect ledger.

An Action's exact version and original proposal are bound before approval.
A committed claim with no receipt is uncertain forever; never retry it.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict

from .action_runtime import _full_url, _request, execute_action
from .actions import ActionRepository
from .agent_runtime import RuntimeSafetyError
from .approval_claims import ApprovalClaimStore
from .approval_runtime import prepare_approval_batch
from .checkpoint_types import LoopSnapshot
from .content_filters import filtered_tool_output, filters_from_snapshot
from .durable_tasks import DurableTaskRepository
from .loop_checkpoint import OwnedLoopCheckpoint
from .machine_permissions import require_machine_scope
from .provider_common import ModelToolCall

ACTION_CAPABILITY = "chat_actions"
_MAX_PROPOSALS = 128


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class ActionEffects:
    def __init__(
        self,
        checkpoint: OwnedLoopCheckpoint,
        *,
        current_grant: Callable[[], tuple[set[str], int | None]],
    ):
        self.checkpoint = checkpoint
        self.lease = checkpoint.lease
        self.current_grant = current_grant
        self.repo = ActionRepository(self.lease.database)

    def _grant(self):
        scopes, expires = self.current_grant()
        require_machine_scope(scopes, "write", expires=expires)
        return scopes, expires

    def _root(self, db):
        self._grant()
        cp, lease = self.checkpoint, self.lease
        lease.assert_current(db)
        if (
            cp.scope is not None
            or lease.detached_task_id is not None
            or lease.wakeup_task_id is not None
        ):
            raise PermissionError(
                "External Action requires an interactive root"
            )
        run = db.execute(
            "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
            (lease.run_id, lease.owner),
        ).fetchone()
        if (
            run is None
            or run["agent_id"] is None
            or db.execute(
                "SELECT 1 FROM agent_run_cancellations WHERE run_id=?",
                (lease.run_id,),
            ).fetchone()
        ):
            raise PermissionError("Action run unavailable")
        state = json.loads(run["state"])
        if state.get("manualCompaction"):
            raise PermissionError(
                "Action requires interactive foreground generation"
            )
        agent = self.repo._bound(db, lease.owner, run["agent_id"])
        if agent is None or json.loads(agent["config"]).get(
            "_version", 1
        ) != state.get("agentRevision"):
            raise PermissionError("Action Agent revision changed")
        return (
            run,
            state,
            {**json.loads(agent["config"]), "id": run["agent_id"]},
        )

    def definitions(self) -> list[dict]:
        with self.lease.database.read_snapshot() as db:
            try:
                _, _, agent = self._root(db)
            except PermissionError:
                return []
        return self.repo.tool_schemas(
            self.lease.owner, agent, non_oauth_only=True
        )

    def _record(self, db, run, call: ModelToolCall):
        record = self.repo._get_for_tool(
            db, self.lease.owner, run["agent_id"], call.name
        )
        if record is None or record[0].get("auth", {}).get("type") == "oauth":
            raise PermissionError("Action no longer selected or requires OAuth")
        _, _, op = record
        path, _, _, _ = _request(op, call.arguments)
        destination = _full_url(op["server"], path)
        if len(destination) > 2048:
            raise ValueError("Action destination exceeds approval limit")
        return record

    def validate(self, call: ModelToolCall) -> None:
        with self.lease.database.read_snapshot() as db:
            run, _, _ = self._root(db)
            self._record(db, run, call)

    def _context(self, db, call: ModelToolCall):
        run, state, _ = self._root(db)
        if state.get("ownedLoopCheckpoint") is not True:
            raise PermissionError(
                "Action requires an owned foreground checkpoint"
            )
        cp = self.checkpoint
        identity = cp.active["configurable"].get("checkpoint_id")
        if not isinstance(identity, str):
            raise PermissionError("Missing Action checkpoint")
        edge = ApprovalClaimStore._edge(db, cp.saver, cp.base, identity)
        snapshot = LoopSnapshot.model_validate(
            edge["checkpoint"]["channel_values"]
        )
        if (
            not snapshot.tool_step_started
            or snapshot.agent_id != run["agent_id"]
            or call not in snapshot.calls()
        ):
            raise PermissionError(
                "Action proposal is not the saved started call"
            )
        record = self._record(db, run, call)
        meta, creds, op = record
        proof = {
            "checkpointId": identity,
            "agentId": run["agent_id"],
            "actionId": op["action_id"],
            "version": op["action_version"],
            "fingerprint": _digest([asdict(call), meta, op]),
        }
        return state, edge, proof, (meta, creds, op)

    def bind(self, call: ModelToolCall) -> str:
        """Commit the live version before displaying a human approval."""
        with self.lease.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state, _, proof, record = self._context(db, call)
            meta, _, op = record
            path, _, _, _ = _request(op, call.arguments)
            description = f"{op['method'].upper()} {_full_url(op['server'], path)} (authentication: {meta.get('auth', {}).get('type', 'none')})"
            proposals = state.setdefault("actionProposals", {})
            if call.call_id in proposals:
                if proposals[call.call_id] != proof:
                    raise PermissionError("Action proposal changed")
                return description
            if len(proposals) >= _MAX_PROPOSALS:
                raise OverflowError("Action proposal limit reached")
            proposals[call.call_id] = proof
            db.execute(
                "UPDATE runs SET state=? WHERE id=?",
                (json.dumps(state, separators=(",", ":")), self.lease.run_id),
            )
            self._grant()
            return description

    def _approved(self, state, edge, proof, call):
        if state.get("actionProposals", {}).get(call.call_id) != proof:
            raise PermissionError(
                "Action version or proposal changed after admission"
            )
        claim = state.get("approvedToolBatch")
        if (
            not isinstance(claim, dict)
            or claim.get("checkpointId") != proof["checkpointId"]
        ):
            raise PermissionError("Action requires explicit human approval")
        ApprovalClaimStore._verify_requests(
            edge, claim["payload"], allow_settled=True
        )
        batch = prepare_approval_batch(claim["payload"], claim["resolutions"])
        entry = next(
            (item for item in batch.entries if item.call_id == call.call_id),
            None,
        )
        if (
            entry is None
            or entry.decision["type"] != "approve"
            or entry.tool_call() != call
        ):
            raise PermissionError("Action must be approved without editing")

    def _claim(self, call, *, result=None, replay_only=False):
        lease = self.lease
        with lease.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state, edge, proof, record = self._context(db, call)
            self._approved(state, edge, proof, call)
            scopes, expires = self._grant()
            fingerprint = _digest(proof)
            machine = "action:" + proof["actionId"]
            prior = db.execute(
                "SELECT * FROM agent_native_effect_calls WHERE run_id=? AND call_id=?",
                (lease.run_id, call.call_id),
            ).fetchone()
            if prior is not None:
                if (
                    prior["fingerprint"] != fingerprint
                    or prior["machine"] != machine
                ):
                    raise PermissionError("Action effect identity changed")
                scopes, expires = DurableTaskRepository._grant(
                    prior, scopes, expires
                )
                require_machine_scope(scopes, "write", expires=expires)
            if db.execute(
                "SELECT 1 FROM agent_durable_tasks WHERE run_id=? AND call_id=? UNION ALL SELECT 1 FROM agent_job_cancel_calls WHERE run_id=? AND call_id=?",
                (lease.run_id, call.call_id, lease.run_id, call.call_id),
            ).fetchone():
                raise PermissionError("Action call identity already used")
            if result is not None:
                if prior is None or prior["receipt"] is not None:
                    raise PermissionError(
                        "Action effect has no unsettled claim"
                    )
                filtered = filtered_tool_output(
                    call.name, result, filters_from_snapshot(db, lease.owner)
                )
                if isinstance(filtered, str):
                    result = {
                        "content": [{"type": "text", "text": filtered}],
                        "isError": True,
                    }
                db.execute(
                    "UPDATE agent_native_effect_calls SET receipt=? WHERE run_id=? AND call_id=?",
                    (
                        json.dumps(result, allow_nan=False),
                        lease.run_id,
                        call.call_id,
                    ),
                )
                self._grant()
                return result, None
            if prior is not None:
                if prior["receipt"] is None:
                    raise RuntimeSafetyError(
                        "External Action outcome is uncertain; do not retry"
                    )
                return json.loads(prior["receipt"]), None
            if replay_only:
                raise PermissionError("Missing settled Action receipt")
            if record[0].get("auth", {}).get("type") == "oauth":
                raise PermissionError(
                    "Action OAuth requires its authorization flow"
                )
            db.execute(
                "INSERT INTO agent_native_effect_calls(run_id,call_id,fingerprint,machine,scopes,grant_expires) VALUES(?,?,?,?,?,?)",
                (
                    lease.run_id,
                    call.call_id,
                    fingerprint,
                    machine,
                    json.dumps(sorted(scopes)),
                    expires,
                ),
            )
            self._grant()
            return None, record

    def receipt(self, call: ModelToolCall) -> dict:
        result, _ = self._claim(call, replay_only=True)
        assert result is not None
        return result

    def authorize_receipt(self, call: ModelToolCall, result: dict) -> None:
        with self.lease.database.read_snapshot() as db:
            claimed = (
                db.execute(
                    "SELECT 1 FROM agent_native_effect_calls WHERE run_id=? AND call_id=?",
                    (self.lease.run_id, call.call_id),
                ).fetchone()
                is not None
            )
        if claimed:
            if self.receipt(call) != result:
                raise PermissionError("Action receipt changed")
        elif result.get("isError") is not True:
            raise PermissionError("Missing settled Action receipt")

    async def execute(self, call: ModelToolCall) -> dict:
        result, record = self._claim(call)
        if result is not None:
            return result
        assert record is not None
        meta, creds, op = record
        # The committed claim cannot be reclaimed after cancellation, network
        # failure, a lost response, or a process crash.
        self._grant()
        self.checkpoint.assert_current()
        try:
            text = await execute_action(meta, creds, op, call.arguments)
        except Exception:
            raise RuntimeSafetyError(
                "External Action outcome is uncertain; do not retry"
            ) from None
        if text.startswith("API call failed ("):
            raise RuntimeSafetyError(
                "External Action outcome is uncertain; do not retry"
            )
        result = {"content": [{"type": "text", "text": text}]}
        settled, _ = self._claim(call, result=result)
        assert settled is not None
        return settled
