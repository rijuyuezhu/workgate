"""SQLite port of LibreChat's owned, generation-scoped LangGraph saver seam.

Source: LibreChat e8f3be08 packages/api/src/agents/checkpointer.ts and
checkpoints/saver.ts; LangGraph checkpoint 1.1.3 WRITES_IDX_MAP. This preserves
checkpoint tuples and pending-write slots, including completed sibling markers.
It does not implement the graph scheduler or promise exactly-once side effects.
The Python runtime supplies JSON message/state values at this storage boundary.
"""

import copy
import json
import logging
import math
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from .checkpoint_types import NAMESPACE_KEY as NAMESPACE_KEY
from .database import SQLiteDatabase
from .generation_actor_lease import ActorLease, ActorLeaseLost
from .graph_member_scope import GraphMemberScope
from .subagent_scope import SubagentGraphScope, SubagentScope

OWNER_KEY = "__librechat_checkpoint_owner"
EVENT_ACTOR_KEY = "__librechat_event_actor_invocation_id"
WRITES_IDX_MAP = {
    "__error__": -1,
    "__scheduled__": -2,
    "__interrupt__": -3,
    "__resume__": -4,
}
DEFAULT_TTL_SECONDS = 24 * 60 * 60
CHECKPOINT_HARD_LIMIT_BYTES = 15 * 1024 * 1024
CHECKPOINT_WARN_BYTES = 8 * 1024 * 1024
log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_checkpoints (
    owner TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    parent_id TEXT,
    checkpoint TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    anchored INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL,
    PRIMARY KEY(owner,thread_id,checkpoint_ns,checkpoint_id)
);
CREATE TABLE IF NOT EXISTS agent_checkpoint_writes (
    owner TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(owner,thread_id,checkpoint_ns,checkpoint_id,task_id,idx),
    FOREIGN KEY(owner,thread_id,checkpoint_ns,checkpoint_id)
      REFERENCES agent_checkpoints(owner,thread_id,checkpoint_ns,checkpoint_id) ON DELETE CASCADE
);
"""


class CheckpointTooLargeError(ValueError):
    code = "CHECKPOINT_TOO_LARGE"

    def __init__(self, size: int, limit: int):
        self.bytes = size
        self.limit = limit
        super().__init__(
            f"Checkpoint state is {size} bytes, over the {limit} byte durable limit"
        )


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def has_resumable_write(writes: Sequence[tuple[str, Any]]) -> bool:
    return any(
        channel == "__interrupt__" or not channel.startswith("__")
        for channel, _ in writes
    )


class SQLiteCheckpointSaver:
    """Owned checkpoint tuples in the Controller's existing SQLite database.

    Ordinary writes use LibreChat's durability=exit anchoring rule. Event-actor
    candidate heads are retained even without an interrupt, as in the original.
    Pending bookkeeping is staged transactionally instead of in process memory;
    readers cannot observe a tuple until its checkpoint is committed. A clean
    exit removes staged rows, and TTL cleanup reclaims abandoned placeholders.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        owner: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        hard_limit_bytes: int = CHECKPOINT_HARD_LIMIT_BYTES,
        warn_bytes: int = CHECKPOINT_WARN_BYTES,
        actor_lease: ActorLease | None = None,
        member_scope: GraphMemberScope
        | SubagentScope
        | SubagentGraphScope
        | None = None,
    ):
        if not isinstance(owner, str) or not owner:
            raise ValueError("Checkpoint owner is required")
        if (
            type(ttl_seconds) not in (int, float)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
            or type(hard_limit_bytes) is not int
            or hard_limit_bytes <= 0
            or type(warn_bytes) is not int
            or warn_bytes <= 0
        ):
            raise ValueError("Checkpoint limits must be positive")
        if actor_lease is not None and (
            actor_lease.owner != owner
            or actor_lease.database.path != database.path
        ):
            raise PermissionError("Checkpoint actor lease owner mismatch")
        self.database = database
        self.owner = owner
        if member_scope is not None and actor_lease is None:
            raise PermissionError(
                "Graph member checkpoints require an actor lease"
            )
        if isinstance(member_scope, SubagentScope) and member_scope.detached:
            raise PermissionError(
                "Detached child requires its own execution lease"
            )
        self.actor_lease = actor_lease
        self.member_scope = member_scope
        self.ttl_seconds = ttl_seconds
        self.hard_limit_bytes = hard_limit_bytes
        self.warn_bytes = warn_bytes
        with database.connect() as db:
            db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nCOMMIT;")

    def config(
        self,
        thread_id: str,
        generation_namespace: str,
        *,
        checkpoint_ns: str = "",
        checkpoint_id: str | None = None,
        event_actor: bool = False,
    ) -> dict:
        result = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            NAMESPACE_KEY: generation_namespace,
            OWNER_KEY: self.owner,
        }
        if checkpoint_id is not None:
            result["checkpoint_id"] = checkpoint_id
        if event_actor:
            result[EVENT_ACTOR_KEY] = generation_namespace
        config = {"configurable": result}
        self._identity(config)
        return config

    def _identity(self, config: Mapping) -> tuple[str, str, str | None]:
        values = config.get("configurable", {})
        if values.get(OWNER_KEY, self.owner) != self.owner:
            raise PermissionError("Checkpoint owner mismatch")
        thread = values.get("thread_id")
        graph_ns = values.get("checkpoint_ns", "")
        generation = values.get(NAMESPACE_KEY)
        checkpoint_id = values.get("checkpoint_id")
        if (
            not isinstance(thread, str)
            or not thread
            or not isinstance(graph_ns, str)
            or not isinstance(generation, str)
            or not generation
            or "|" in generation
            or (
                checkpoint_id is not None
                and (not isinstance(checkpoint_id, str) or not checkpoint_id)
            )
        ):
            raise ValueError("Invalid owned checkpoint identity")
        return (
            thread,
            generation + ("|" + graph_ns if graph_ns else ""),
            checkpoint_id,
        )

    def _assert_write(
        self,
        db: sqlite3.Connection,
        thread: str,
        namespace: str,
        *,
        cleanup: bool = False,
    ) -> None:
        """Fence foreground writes against the owning run and conversation."""
        generation = namespace.split("|", 1)[0]
        child_namespace = ":child:" in generation
        run_id = generation.split(":graph:", 1)[0].split(":child:", 1)[0]
        row = db.execute(
            "SELECT r.conversation_id,r.status,c.owner FROM runs r "
            "JOIN conversations c ON c.id=r.conversation_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        lease = self.actor_lease
        if lease is None:
            # Standalone saver namespaces do not have an HTTP generation. A
            # persisted run may not use that path to bypass its actor fence.
            if row is None and not child_namespace:
                return
            if (
                cleanup
                and row is not None
                and row["owner"] == self.owner
                and row["conversation_id"] == thread
                and row["status"]
                in {"complete", "aborted", "error", "interrupted"}
            ):
                return
            raise ActorLeaseLost(
                "Generation checkpoint write requires its actor lease"
            )
        if (
            lease.run_id != run_id
            or row is None
            or row["conversation_id"] != thread
        ):
            raise ActorLeaseLost(
                "Checkpoint identity does not match generation actor lease"
            )
        lease.assert_current(db)
        if child_namespace and not isinstance(
            self.member_scope, (SubagentScope, SubagentGraphScope)
        ):
            raise ActorLeaseLost(
                "Child checkpoint requires its admitted delegation scope"
            )
        if self.member_scope is not None:
            if namespace != self.member_scope.checkpoint_namespace:
                raise ActorLeaseLost(
                    "Checkpoint namespace does not match scoped activation"
                    if isinstance(
                        self.member_scope, (SubagentScope, SubagentGraphScope)
                    )
                    else "Checkpoint namespace does not match graph member activation"
                )
            self.member_scope.resolve(db, lease)

    def _check_size(self, db: sqlite3.Connection, key: tuple) -> None:
        """The durable limit covers the stored tuple and its pending writes."""
        row = db.execute(
            "SELECT coalesce(length(CAST(checkpoint AS BLOB)),0)+length(CAST(metadata AS BLOB)) "
            "FROM agent_checkpoints WHERE owner=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
            key,
        ).fetchone()
        if row is None:
            return
        writes = db.execute(
            "SELECT coalesce(sum(length(CAST(value AS BLOB))),0) FROM agent_checkpoint_writes "
            "WHERE owner=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
            key,
        ).fetchone()[0]
        size = row[0] + writes
        if size > self.hard_limit_bytes:
            raise CheckpointTooLargeError(size, self.hard_limit_bytes)

    def _result_config(self, requested: Mapping, checkpoint_id: str) -> dict:
        result = copy.deepcopy(dict(requested))
        result["configurable"] = {
            **result.get("configurable", {}),
            OWNER_KEY: self.owner,
            "checkpoint_id": checkpoint_id,
        }
        return result

    def put_writes(
        self, config: Mapping, writes: Sequence[tuple[str, Any]], task_id: str
    ) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._put_writes(db, config, writes, task_id)

    def _put_writes(
        self,
        db: sqlite3.Connection,
        config: Mapping,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        thread, namespace, checkpoint_id = self._identity(config)
        if checkpoint_id is None:
            raise ValueError("Owned writes require a checkpoint id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Checkpoint task id is required")
        if any(not isinstance(channel, str) for channel, _ in writes):
            raise ValueError("Checkpoint channels must be strings")
        # Serialize every value before taking the transaction: one invalid value
        # must never leave a partial sibling batch marked as complete.
        rows = [
            (
                self.owner,
                thread,
                namespace,
                checkpoint_id,
                task_id,
                WRITES_IDX_MAP.get(channel, index),
                channel,
                _json(value),
            )
            for index, (channel, value) in enumerate(writes)
        ]
        anchored = int(has_resumable_write(writes))
        all_special = all(channel in WRITES_IDX_MAP for channel, _ in writes)
        self._assert_write(db, thread, namespace)
        db.execute(
            "INSERT INTO agent_checkpoints(owner,thread_id,checkpoint_ns,checkpoint_id,anchored,expires_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(owner,thread_id,checkpoint_ns,checkpoint_id) DO UPDATE SET "
            "anchored=max(anchored,excluded.anchored)",
            (
                self.owner,
                thread,
                namespace,
                checkpoint_id,
                anchored,
                time.time() + self.ttl_seconds,
            ),
        )
        conflict = (
            "DO UPDATE SET channel=excluded.channel,value=excluded.value"
            if all_special
            else "DO NOTHING"
        )
        db.executemany(
            "INSERT INTO agent_checkpoint_writes VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(owner,thread_id,checkpoint_ns,checkpoint_id,task_id,idx) "
            + conflict,
            rows,
        )
        self._check_size(db, (self.owner, thread, namespace, checkpoint_id))

    def put(
        self, config: Mapping, checkpoint: Mapping, metadata: Mapping
    ) -> dict:
        thread, namespace, parent_id = self._identity(config)
        checkpoint_id = checkpoint.get("id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("Checkpoint id is required")
        state, meta = _json(checkpoint), _json(metadata)
        size = len(state.encode("utf-8")) + len(meta.encode("utf-8"))
        if size > self.hard_limit_bytes:
            raise CheckpointTooLargeError(size, self.hard_limit_bytes)
        if size >= self.warn_bytes:
            log.warning(
                "Durable checkpoint exceeds soft size threshold: %s bytes", size
            )
        key = self.owner, thread, namespace, checkpoint_id
        event_actor = config.get("configurable", {}).get(EVENT_ACTOR_KEY)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_write(db, thread, namespace)
            existing = db.execute(
                "SELECT anchored FROM agent_checkpoints WHERE "
                "owner=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                key,
            ).fetchone()
            if not event_actor and (
                existing is None or not existing["anchored"]
            ):
                db.execute(
                    "DELETE FROM agent_checkpoints WHERE owner=? AND thread_id=? "
                    "AND checkpoint_ns=? AND checkpoint_id=?",
                    key,
                )
            else:
                db.execute(
                    "INSERT INTO agent_checkpoints VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(owner,thread_id,checkpoint_ns,checkpoint_id) DO UPDATE SET "
                    "parent_id=excluded.parent_id,checkpoint=excluded.checkpoint,metadata=excluded.metadata,"
                    "expires_at=excluded.expires_at",
                    (
                        *key,
                        parent_id,
                        state,
                        meta,
                        1,
                        time.time() + self.ttl_seconds,
                    ),
                )
                self._check_size(db, key)
        return self._result_config(config, checkpoint_id)

    def _tuple(self, db: Any, row: Any, requested: Mapping) -> dict:
        writes = db.execute(
            "SELECT task_id,channel,value FROM agent_checkpoint_writes WHERE "
            "owner=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=? ORDER BY task_id,idx",
            (
                self.owner,
                row["thread_id"],
                row["checkpoint_ns"],
                row["checkpoint_id"],
            ),
        ).fetchall()
        result = {
            "config": self._result_config(requested, row["checkpoint_id"]),
            "checkpoint": json.loads(row["checkpoint"]),
            "metadata": json.loads(row["metadata"]),
            "pendingWrites": [
                (write["task_id"], write["channel"], json.loads(write["value"]))
                for write in writes
            ],
        }
        if row["parent_id"] is not None:
            result["parentConfig"] = self._result_config(
                requested, row["parent_id"]
            )
        return result

    def get_tuple(self, config: Mapping) -> dict | None:
        with self.database.read_snapshot() as db:
            return self._get_tuple(db, config)

    def _get_tuple(
        self, db: sqlite3.Connection, config: Mapping
    ) -> dict | None:
        thread, namespace, checkpoint_id = self._identity(config)
        if (
            isinstance(self.member_scope, (SubagentScope, SubagentGraphScope))
            or ":child:" in namespace.split("|", 1)[0]
        ):
            self._assert_write(db, thread, namespace)
        parameters: list[Any] = [self.owner, thread, namespace, time.time()]
        query = "SELECT * FROM agent_checkpoints WHERE owner=? AND thread_id=? AND checkpoint_ns=? "
        query += "AND expires_at>? AND checkpoint IS NOT NULL"
        if checkpoint_id is not None:
            query += " AND checkpoint_id=?"
            parameters.append(checkpoint_id)
        row = db.execute(
            query + " ORDER BY checkpoint_id DESC LIMIT 1", parameters
        ).fetchone()
        return self._tuple(db, row, config) if row else None

    def list(
        self,
        config: Mapping,
        *,
        before: Mapping | None = None,
        limit: int | None = None,
        filter: Mapping | None = None,
    ) -> Iterator[dict]:
        thread, namespace, _ = self._identity(config)
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("Invalid checkpoint list limit")
        filters = dict(filter or {})
        if any(
            isinstance(value, (Mapping, list)) for value in filters.values()
        ):
            raise ValueError("Checkpoint metadata filters must be primitive")
        boundary = None
        if before is not None:
            before_thread, before_namespace, boundary = self._identity(before)
            if boundary is None or (before_thread, before_namespace) != (
                thread,
                namespace,
            ):
                raise ValueError("Invalid checkpoint list boundary")
        if limit == 0:
            return
        with self.database.read_snapshot() as db:
            if (
                isinstance(
                    self.member_scope, (SubagentScope, SubagentGraphScope)
                )
                or ":child:" in namespace.split("|", 1)[0]
            ):
                self._assert_write(db, thread, namespace)
            rows = db.execute(
                "SELECT * FROM agent_checkpoints WHERE owner=? AND thread_id=? AND checkpoint_ns=? "
                "AND expires_at>? AND checkpoint IS NOT NULL ORDER BY checkpoint_id DESC",
                (self.owner, thread, namespace, time.time()),
            )
            results = []
            for row in rows:
                if boundary is not None and row["checkpoint_id"] >= boundary:
                    continue
                metadata = json.loads(row["metadata"])
                if not all(
                    metadata.get(key) == value for key, value in filters.items()
                ):
                    continue
                results.append(self._tuple(db, row, config))
                if limit is not None and len(results) >= limit:
                    break
        yield from results

    def has_durable_interrupt(self, config: Mapping, interrupt_id: str) -> bool:
        if not interrupt_id or self._identity(config)[2] is None:
            return False
        result = self.get_tuple(config)
        if result is None:
            return False
        for _, channel, value in result["pendingWrites"]:
            if channel == "__interrupt__":
                values = value if isinstance(value, list) else [value]
                if any(
                    isinstance(item, dict) and item.get("id") == interrupt_id
                    for item in values
                ):
                    return True
        return False

    def delete_generation(self, config: Mapping) -> int:
        thread, _, _ = self._identity(config)
        namespace = config["configurable"][NAMESPACE_KEY]
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_write(db, thread, namespace, cleanup=True)
            cursor = db.execute(
                "DELETE FROM agent_checkpoints WHERE owner=? AND thread_id=? "
                "AND (checkpoint_ns=? OR substr(checkpoint_ns,1,?)=?)",
                (
                    self.owner,
                    thread,
                    namespace,
                    len(namespace) + 1,
                    namespace + "|",
                ),
            )
            return cursor.rowcount

    def prune_expired(self) -> int:
        with self.database.connect() as db:
            return db.execute(
                "DELETE FROM agent_checkpoints WHERE owner=? AND expires_at<=?",
                (self.owner, time.time()),
            ).rowcount
