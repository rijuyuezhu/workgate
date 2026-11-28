"""Trusted publication binding for an explicitly admitted public native call.

Adapted from pinned v6.0.3 tool_attachments.py. WorkGate proves the active
native loop edge rather than trusting a model-visible tool result or UI event.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from .attachment_history import _attachment
from .attachment_uploads import (
    MAX_ATTACHMENT_BYTES,
    _identity,
    _matches,
    _safe_path,
)
from .checkpoint_types import LoopSnapshot
from .generation_protocol import EVENT_LIMIT, MAX_EVENT_BYTES
from .loop_checkpoint import OwnedLoopCheckpoint
from .provider_common import ModelToolCall

TOOL_NAME = "publish_chat_attachment"
_MAX_PUBLICATIONS = 128


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class ToolAttachmentPublication:
    """An internal bind callback, never an API accepting arbitrary file IDs.

    The trusted publisher calls lookup before acquiring remote bytes and bind
    only from the existing journaled publication transaction. Its live native
    authorization check remains mandatory, including on replay.
    """

    def __init__(
        self,
        checkpoint: OwnedLoopCheckpoint,
        proposal: ModelToolCall,
        request: ModelToolCall,
        *,
        check_grant: Callable[[], None],
    ):
        lease = checkpoint.lease
        if (
            checkpoint.scope is not None
            or lease.detached_task_id is not None
            or lease.wakeup_task_id is not None
            or proposal.name != TOOL_NAME
            or request.name != TOOL_NAME
            or proposal.call_id != request.call_id
            or not 0 < len(proposal.call_id) <= 200
        ):
            raise PermissionError(
                "Tool attachment requires an explicit public root publication"
            )
        self.check_grant = check_grant
        self.checkpoint = checkpoint
        self.lease = lease
        self.call_id = proposal.call_id
        self.proposal_digest = _digest(asdict(proposal))
        self.request_digest = _digest(asdict(request))

    def _edge(self, db: sqlite3.Connection):
        self.lease.assert_current(db)
        run = db.execute(
            "SELECT r.*,c.owner FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
            (self.lease.run_id, self.lease.owner),
        ).fetchone()
        if run is None or run["status"] != "running":
            raise PermissionError("Tool attachment run is not active or owned")
        row = self.checkpoint.saver._get_tuple(db, self.checkpoint.base)
        if (
            row is None
            or row["config"] != self.checkpoint.active
            or row["metadata"].get("source") != "loop"
            or row["checkpoint"].get("v") != 1
        ):
            raise PermissionError(
                "Tool attachment checkpoint is no longer current"
            )
        snapshot = LoopSnapshot.model_validate(
            row["checkpoint"]["channel_values"]
        )
        if (
            not snapshot.tool_step_started
            or snapshot.agent_id != run["agent_id"]
            or not any(
                _digest(asdict(call)) == self.proposal_digest
                for call in snapshot.calls()
            )
        ):
            raise PermissionError(
                "Tool attachment call is not the saved started proposal"
            )
        state = json.loads(run["state"])
        if (
            state.get("manualCompaction")
            or state.get("ownedLoopCheckpoint") is not True
        ):
            raise PermissionError(
                "Tool attachment requires a public native loop"
            )
        proof = {
            "checkpointId": row["config"]["configurable"]["checkpoint_id"],
            "proposal": self.proposal_digest,
            "request": self.request_digest,
        }
        return run, state, proof

    def _record(self, db, run, state, file_id):
        row = db.execute(
            "SELECT * FROM attachments WHERE id=? AND owner=? AND NOT EXISTS (SELECT 1 FROM attachment_deletions WHERE file_id=?)",
            (file_id, self.lease.owner, file_id),
        ).fetchone()
        if row is None:
            raise PermissionError("Published tool attachment is unavailable")
        metadata = json.loads(row["metadata"])
        size, digest = metadata.get("bytes"), metadata.get("sha256")
        if (
            type(size) is not int
            or not 0 < size <= MAX_ATTACHMENT_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise OSError("Tool attachment has no verified bytes")
        path = _safe_path(self.lease.database, Path(row["path"]))
        if not _matches(path, _identity(path.lstat()), size, digest):
            raise OSError("Published tool attachment integrity changed")
        return _attachment(
            row,
            state["responseMessageId"],
            self.call_id,
            run["conversation_id"],
        )

    def lookup(self) -> dict | None:
        """Replay the exact publication, or fail closed after deletion/change."""
        self.check_grant()
        with self.lease.database.read_snapshot() as db:
            run, state, proof = self._edge(db)
            prior = state.get("toolAttachmentPublications", {}).get(
                self.call_id
            )
            if prior is None:
                return None
            file_id = prior.get("fileId")
            if (
                prior != {**proof, "fileId": file_id}
                or not db.execute(
                    "SELECT 1 FROM tool_attachment_links WHERE run_id=? AND response_message_id=? AND tool_call_id=? AND file_id=?",
                    (
                        self.lease.run_id,
                        state["responseMessageId"],
                        self.call_id,
                        file_id,
                    ),
                ).fetchone()
            ):
                raise PermissionError(
                    "Tool attachment publication identity changed or was retired"
                )
            result = self._record(db, run, state, file_id)
            self.check_grant()
            return result

    def bind(self, db: sqlite3.Connection, file_id: str) -> dict:
        """Commit attachment association, replay proof and event atomically."""
        self.check_grant()
        run, state, proof = self._edge(db)
        publications = state.setdefault("toolAttachmentPublications", {})
        if (
            self.call_id in publications
            or len(publications) >= _MAX_PUBLICATIONS
        ):
            raise PermissionError(
                "Tool attachment publication already settled or exceeds limit"
            )
        record = self._record(db, run, state, file_id)
        if db.execute(
            "SELECT 1 FROM tool_attachment_links WHERE file_id=?", (file_id,)
        ).fetchone():
            raise PermissionError(
                "File is already associated with a tool publication"
            )
        event = json.dumps(
            {"event": "attachment", "data": record}, separators=(",", ":")
        )
        if (
            len(event.encode()) > MAX_EVENT_BYTES
            or db.execute(
                "SELECT count(*) FROM generation_events WHERE run_id=?",
                (self.lease.run_id,),
            ).fetchone()[0]
            >= EVENT_LIMIT
        ):
            raise ValueError("Tool attachment event retention limit reached")
        db.execute(
            "INSERT INTO tool_attachment_links(run_id,response_message_id,tool_call_id,file_id) VALUES(?,?,?,?)",
            (
                self.lease.run_id,
                state["responseMessageId"],
                self.call_id,
                file_id,
            ),
        )
        publications[self.call_id] = {**proof, "fileId": file_id}
        db.execute(
            "UPDATE runs SET state=? WHERE id=?",
            (json.dumps(state, separators=(",", ":")), self.lease.run_id),
        )
        db.execute(
            "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
            (self.lease.run_id, event),
        )
        self.check_grant()
        return record
