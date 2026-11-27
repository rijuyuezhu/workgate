"""Immutable foreground graph activation identity; no scheduler or grant."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from uuid import UUID

from .generation_actor_lease import ActorLease
from .graph_topology import graph_members


@dataclass(frozen=True)
class GraphMemberScope:
    graph_namespace: str
    root_id: str
    root_version: int
    member_id: str
    activation_id: str

    @property
    def checkpoint_namespace(self) -> str:
        return self.graph_namespace + ":member:" + self.activation_id

    @property
    def marker(self) -> list:
        return [self.root_id, self.root_version, self.member_id]

    def native_call_id(self, call_id: str) -> str:
        """Keep provider correlation IDs out of native cross-activation identity."""
        return (
            "graph:"
            + hashlib.sha256(
                json.dumps(
                    [self.checkpoint_namespace, self.marker, call_id],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )

    def resolve(
        self, db: sqlite3.Connection, lease: ActorLease
    ) -> tuple[dict, dict]:
        lease.assert_current(db)
        if (
            not self.graph_namespace.startswith(lease.run_id + ":graph:")
            or "|" in self.graph_namespace
        ):
            raise PermissionError(
                "Graph activation namespace does not belong to this run"
            )
        if str(UUID(self.activation_id)) != self.activation_id:
            raise ValueError("Invalid graph activation identity")
        run = db.execute(
            "SELECT agent_id,state FROM runs WHERE id=?", (lease.run_id,)
        ).fetchone()
        if run is None or run["agent_id"] != self.root_id:
            raise PermissionError(
                "Graph activation root does not match this generation"
            )
        state = json.loads(run["state"])
        if self.graph_namespace not in state.get("ownedGraphCheckpoints", {}):
            raise PermissionError(
                "Graph activation has no admitted graph checkpoint"
            )
        marker = state.get("ownedGraphMemberCheckpoints", {}).get(
            self.checkpoint_namespace
        )
        if marker is not None and marker != self.marker:
            raise PermissionError(
                "Graph activation checkpoint identity changed"
            )

        def agent(identity: str) -> dict:
            row = db.execute(
                "SELECT config FROM agents WHERE id=? AND owner=?",
                (identity, lease.owner),
            ).fetchone()
            if row is None:
                raise PermissionError(
                    "Graph activation Agent is no longer owned"
                )
            return {**json.loads(row["config"]), "id": identity}

        root = agent(self.root_id)
        if root.get(
            "_version", 1
        ) != self.root_version or self.member_id not in graph_members(root):
            raise PermissionError(
                "Graph activation definition or membership changed"
            )
        return root, agent(self.member_id)
