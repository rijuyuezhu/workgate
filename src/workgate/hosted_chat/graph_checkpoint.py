"""Python LangGraph saver adapter over the existing owner-scoped SQLite tuples.

The optional dependency is imported by the WebUI graph path, never by Worker.
LangGraph 1.0 checkpoints contain `Send` and other non-JSON types: store every
channel and write via the SDK serializer rather than lossy JSON conversion.
"""

import base64
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Interrupt

from .checkpoints import EVENT_ACTOR_KEY, NAMESPACE_KEY, SQLiteCheckpointSaver
from .database import SQLiteDatabase
from .generation_actor_lease import ActorLease, ActorLeaseLost


def verify_graph_interrupt(
    db: sqlite3.Connection,
    storage: SQLiteCheckpointSaver,
    thread: str,
    namespace: str,
    activation_id: str,
    interrupt_id: str,
    request: dict,
) -> None:
    """Fence a human action against the latest durable SDK task interruption.

    This read also works while the generation is parked without an actor.
    """
    row = storage._get_tuple(
        db, storage.config(thread, namespace, event_actor=True)
    )
    if row is not None:
        for task, channel, value in row["pendingWrites"]:
            if task != activation_id or channel != "__interrupt__":
                continue
            items = JsonPlusSerializer().loads_typed(
                (value["type"], base64.b64decode(value["data"]))
            )
            if isinstance(items, list) and any(
                isinstance(item, Interrupt)
                and item.id == interrupt_id
                and item.value == request
                for item in items
            ):
                return
    raise ValueError("Graph human action has no matching durable SDK interrupt")


class OwnedGraphCheckpointer(BaseCheckpointSaver):
    def __init__(
        self, database: SQLiteDatabase, owner: str, *, actor_lease: ActorLease
    ):
        super().__init__()
        self.storage = SQLiteCheckpointSaver(
            database, owner, actor_lease=actor_lease
        )
        self.lease = actor_lease
        with database.read_snapshot() as db:
            actor_lease.assert_current(db)
            row = db.execute(
                "SELECT conversation_id FROM runs WHERE id=?",
                (actor_lease.run_id,),
            ).fetchone()
            if row is None:
                raise ActorLeaseLost("Generation no longer exists")
            self.thread_id = row["conversation_id"]

    def _guard(self, config: RunnableConfig) -> None:
        thread, _, _ = self.storage._identity(config)
        values = config.get("configurable", {})
        generation = values[NAMESPACE_KEY]
        if (
            thread != self.thread_id
            or not generation.startswith(self.lease.run_id + ":graph:")
            or values.get(EVENT_ACTOR_KEY) != generation
        ):
            raise ActorLeaseLost(
                "Graph checkpoint identity does not match its actor"
            )
        self.lease.assert_current()

    def _encode(self, value: Any) -> dict:
        kind, data = self.serde.dumps_typed(value)
        return {"type": kind, "data": base64.b64encode(data).decode("ascii")}

    def _decode(self, value: dict) -> Any:
        return self.serde.loads_typed(
            (value["type"], base64.b64decode(value["data"]))
        )

    def _tuple(self, row: dict | None) -> CheckpointTuple | None:
        if row is None:
            return None
        return CheckpointTuple(
            row["config"],
            self._decode(row["checkpoint"]["typed"]),
            self._decode(row["metadata"]["typed"]),
            row.get("parentConfig"),
            [
                (task, channel, self._decode(value))
                for task, channel, value in row["pendingWrites"]
            ],
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._guard(config)
        result = self._tuple(self.storage.get_tuple(config))
        self.lease.assert_current()
        return result

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            raise ValueError(
                "Owner-scoped checkpoint enumeration requires a thread config"
            )
        self._guard(config)
        if before is not None:
            self._guard(before)
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("Invalid checkpoint list limit")
        if limit == 0:
            return
        # Metadata is typed, so match decoded values before applying limit.
        count = 0
        for row in self.storage.list(config, before=before):
            self.lease.assert_current()
            result = self._tuple(row)
            if result is not None and all(
                result.metadata.get(key) == value
                for key, value in (filter or {}).items()
            ):
                yield result
                count += 1
                if limit is not None and count >= limit:
                    return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._guard(config)
        return cast(
            RunnableConfig,
            self.storage.put(
                config,
                {"id": checkpoint["id"], "typed": self._encode(checkpoint)},
                {"typed": self._encode(metadata)},
            ),
        )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._guard(config)
        self.storage.put_writes(
            config,
            [(channel, self._encode(value)) for channel, value in writes],
            task_id=task_id,
        )

    async def aget_tuple(
        self, config: RunnableConfig
    ) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(
            config, filter=filter, before=before, limit=limit
        ):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)
