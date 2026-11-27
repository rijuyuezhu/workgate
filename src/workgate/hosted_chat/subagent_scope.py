"""Owned foreground child identity bound to a durable parent tool edge.

No separate scheduler: the parent generation actor owns foreground child
checkpoints. Detached children require a separate durable lease.
"""

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from .checkpoint_types import NAMESPACE_KEY, LoopSnapshot
from .generation_actor_lease import ActorLease
from .graph_member_scope import GraphMemberScope
from .graph_topology import graph_members
from .provider_common import ModelToolCall
from .subagents import (
    MAX_SUBAGENT_DEPTH,
    SUBAGENT_TOOL_NAME,
    SubagentInvocation,
)


class _ParentCheckpoint(Protocol):
    @property
    def lease(self) -> ActorLease: ...

    @property
    def active(self) -> dict: ...

    @property
    def scope(
        self,
    ) -> GraphMemberScope | SubagentScope | SubagentGraphScope | None: ...


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _agent(db: sqlite3.Connection, owner: str, identity: str) -> dict:
    row = db.execute(
        "SELECT config FROM agents WHERE owner=? AND id=?", (owner, identity)
    ).fetchone()
    if row is None:
        raise PermissionError("Subagent Agent is no longer owned")
    return {**json.loads(row["config"]), "id": identity}


def _revision(agent: dict | None) -> tuple[str, int] | None:
    return (
        (agent["id"], agent.get("_version", 1)) if agent is not None else None
    )


@dataclass(frozen=True)
class SubagentScope:
    run_id: str
    parent_scope: GraphMemberScope | SubagentScope | None
    parent_checkpoint_id: str
    call_id: str
    proposal_hash: str
    invocation_hash: str
    parent_revision: tuple[str, int] | None
    target_revisions: tuple[tuple[str, int], ...]
    remaining_depth: int
    scopes: tuple[str, ...]
    grant_expires: int | None

    @property
    def parent_namespace(self) -> str:
        return (
            self.parent_scope.checkpoint_namespace
            if self.parent_scope is not None
            else self.run_id
        )

    @property
    def checkpoint_namespace(self) -> str:
        # Provider call IDs can repeat on another round or a sibling activation.
        return (
            self.run_id
            + ":child:"
            + _digest(
                [self.parent_namespace, self.parent_checkpoint_id, self.call_id]
            )
        )

    @property
    def member_id(self) -> str | None:
        return self.target_revisions[0][0] if self.target_revisions else None

    @property
    def marker(self) -> list:
        return [self.proposal_hash, self.invocation_hash]

    def record(self) -> dict:
        return json.loads(_json(asdict(self)))

    def native_call_id(self, call_id: str) -> str:
        return "child:" + _digest([self.checkpoint_namespace, call_id])

    def effective_grant(
        self, scopes: set[str] | frozenset[str], expires: int | None
    ) -> tuple[set[str], int | None]:
        if not set(self.scopes).issubset(scopes):
            raise PermissionError("Subagent authentication grant was reduced")
        expires = min(
            (
                value
                for value in (expires, self.grant_expires)
                if value is not None
            ),
            default=None,
        )
        if expires is not None and expires <= time.time():
            raise PermissionError("Subagent authentication grant expired")
        return set(self.scopes), expires

    @classmethod
    def from_record(cls, value: dict) -> SubagentScope:
        """Recover only the durable scope descriptor; resolve it before use."""
        fields = dict(value)
        parent = fields["parent_scope"]
        if parent is not None:
            fields["parent_scope"] = (
                cls.from_record(parent)
                if "run_id" in parent
                else GraphMemberScope(**parent)
            )
        if fields["parent_revision"] is not None:
            fields["parent_revision"] = tuple(fields["parent_revision"])
        fields["target_revisions"] = tuple(
            tuple(item) for item in fields["target_revisions"]
        )
        fields["scopes"] = tuple(fields["scopes"])
        scope = cls(**fields)
        if scope.record() != value:
            raise PermissionError("Invalid subagent scope record")
        return scope

    def _run(self, db: sqlite3.Connection, owner: str, run_id: str):
        if run_id != self.run_id:
            raise PermissionError(
                "Subagent scope belongs to a different generation"
            )
        row = db.execute(
            "SELECT r.conversation_id,r.agent_id,r.state FROM runs r "
            "JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
            (run_id, owner),
        ).fetchone()
        if row is None:
            raise PermissionError("Subagent generation is no longer owned")
        return row

    def _assert_edge(self, db: sqlite3.Connection, owner: str, run) -> None:
        # Read the exact current parent edge inside the same transaction as
        # admission/effect fencing. Expired, missing or advanced heads cannot
        # be used to start a new child.
        row = db.execute(
            "SELECT checkpoint_id,checkpoint,metadata FROM agent_checkpoints "
            "WHERE owner=? AND thread_id=? AND checkpoint_ns=? AND expires_at>? "
            "AND checkpoint IS NOT NULL ORDER BY checkpoint_id DESC LIMIT 1",
            (
                owner,
                run["conversation_id"],
                self.parent_namespace,
                time.time(),
            ),
        ).fetchone()
        if row is None or row["checkpoint_id"] != self.parent_checkpoint_id:
            raise PermissionError(
                "Subagent parent tool edge is no longer current"
            )
        checkpoint = json.loads(row["checkpoint"])
        if (
            checkpoint.get("v") != 1
            or json.loads(row["metadata"]).get("source") != "loop"
        ):
            raise PermissionError(
                "Subagent parent is not a committed loop checkpoint"
            )
        snapshot = LoopSnapshot.model_validate(checkpoint["channel_values"])
        if (
            not snapshot.tool_step_started
            or snapshot.agent_id
            != (self.parent_revision[0] if self.parent_revision else None)
            or not any(
                call.call_id == self.call_id
                and call.name == SUBAGENT_TOOL_NAME
                and _digest(asdict(call)) == self.proposal_hash
                for call in snapshot.calls()
            )
        ):
            raise PermissionError(
                "Subagent proposal does not match the started parent edge"
            )

    def _validate(
        self, db: sqlite3.Connection, owner: str, run, *, current_edges: bool
    ) -> tuple[dict | None, ...]:
        self.effective_grant(set(self.scopes), self.grant_expires)
        if self.parent_scope is None:
            parent = (
                _agent(db, owner, run["agent_id"])
                if run["agent_id"] is not None
                else None
            )
            chain = (parent,)
        elif isinstance(self.parent_scope, SubagentScope):
            chain = self.parent_scope.resolve_owned(
                db, owner, self.run_id, current_edges=current_edges
            )
        else:
            chain = self.parent_scope.resolve_owned(db, owner, self.run_id)
        if _revision(chain[-1]) != self.parent_revision:
            raise PermissionError("Subagent parent revision changed")
        if current_edges:
            self._assert_edge(db, owner, run)
        child = None
        for identity, version in self.target_revisions:
            member = _agent(db, owner, identity)
            if _revision(member) != (identity, version):
                raise PermissionError("Subagent target revision changed")
            if identity == self.member_id:
                child = member
        return (*chain, child)

    def resolve_owned(
        self,
        db: sqlite3.Connection,
        owner: str,
        run_id: str,
        *,
        current_edges: bool = False,
    ) -> tuple[dict | None, ...]:
        """Recheck original ownership and ACL provenance for historical cleanup.

        Cleanup can inspect the admitted chain after the parent advances. It
        cannot authorize an effect; foreground execution requires resolve().
        """
        run = self._run(db, owner, run_id)
        state = json.loads(run["state"])
        if (
            state.get("ownedSubagentScopes", {}).get(self.checkpoint_namespace)
            != self.record()
        ):
            raise PermissionError("Subagent scope has no matching admission")
        return self._validate(db, owner, run, current_edges=current_edges)

    def resolve(
        self, db: sqlite3.Connection, lease: ActorLease
    ) -> tuple[dict | None, ...]:
        lease.assert_current(db)
        return self.resolve_owned(
            db, lease.owner, lease.run_id, current_edges=True
        )

    @classmethod
    def admit(
        cls,
        checkpoint: _ParentCheckpoint,
        call: ModelToolCall,
        invocation: SubagentInvocation,
        *,
        disclosure_access: dict | None = None,
    ) -> SubagentScope:
        lease = checkpoint.lease
        active = checkpoint.active["configurable"]
        identity = active.get("checkpoint_id")
        if (
            not isinstance(identity, str)
            or not identity
            or invocation.owner != lease.owner
            or invocation.parent_run_id != lease.run_id
            or invocation.conversation_id != active["thread_id"]
            or invocation.parent_tool_call_id != call.call_id
            or call.name != SUBAGENT_TOOL_NAME
            or not isinstance(call.arguments, dict)
            or call.arguments.get("run_in_background") is True
            or invocation.subagent_type != call.arguments.get("subagent_type")
            or invocation.parent_agent_id
            != (
                invocation.parent_revision[0]
                if invocation.parent_revision
                else None
            )
            or not 0 <= invocation.remaining_depth < MAX_SUBAGENT_DEPTH
        ):
            raise PermissionError("Invalid subagent invocation identity")
        description = call.arguments.get("description")
        if not isinstance(description, str) or not description.strip():
            description = "No task description provided"
        if (
            len(invocation.messages) not in (1, 2)
            or invocation.messages[-1]
            != {"role": "user", "content": description}
            or (
                len(invocation.messages) == 2
                and invocation.messages[0].get("role") != "system"
            )
        ):
            raise PermissionError(
                "Subagent input does not match the delegated task"
            )
        parent = checkpoint.scope
        if isinstance(parent, SubagentGraphScope):
            raise PermissionError("Team graph member delegation is not enabled")
        expires = invocation.grant_expires
        if isinstance(parent, SubagentScope):
            if (
                invocation.remaining_depth >= parent.remaining_depth
                or not invocation.scopes.issubset(parent.scopes)
            ):
                raise PermissionError(
                    "Subagent child exceeds parent scope or nesting budget"
                )
            expires = min(
                (
                    value
                    for value in (expires, parent.grant_expires)
                    if value is not None
                ),
                default=None,
            )
        fingerprint = asdict(invocation)
        # Recovery may arrive with a fresh authenticated grant, but must never
        # change child input or enlarge the originally admitted grant.
        fingerprint.pop("scopes")
        fingerprint.pop("grant_expires")
        # A completed private child may be recovered before the parent receipt
        # is committed. Bind the original disclosure ceiling at admission.
        if disclosure_access is not None:
            fingerprint["disclosure_access"] = disclosure_access
        with lease.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease.assert_current(db)
            row = db.execute(
                "SELECT conversation_id,agent_id,state FROM runs WHERE id=?",
                (lease.run_id,),
            ).fetchone()
            if (
                row is None
                or row["conversation_id"] != invocation.conversation_id
            ):
                raise PermissionError("Subagent conversation changed")
            if invocation.ordinary_chat:
                if (
                    invocation.agent
                    or invocation.parent_revision is not None
                    or invocation.subagent_type != "self"
                    or invocation.remaining_depth != 0
                ):
                    raise PermissionError("Invalid ordinary chat child")
                revisions = ()
            else:
                revisions = tuple(
                    (member, _agent(db, lease.owner, member).get("_version", 1))
                    for member in graph_members(invocation.agent)
                )
                if revisions[0] != (
                    invocation.agent["id"],
                    invocation.agent.get("version"),
                ):
                    raise PermissionError("Subagent target revision changed")
            scope = cls(
                lease.run_id,
                parent,
                identity,
                call.call_id,
                _digest(asdict(call)),
                _digest(fingerprint),
                invocation.parent_revision,
                revisions,
                invocation.remaining_depth,
                tuple(sorted(invocation.scopes)),
                expires,
            )
            if active.get(NAMESPACE_KEY) != scope.parent_namespace:
                raise PermissionError(
                    "Subagent parent checkpoint namespace changed"
                )
            state = json.loads(row["state"])
            markers = state.setdefault("ownedSubagentScopes", {})
            existing = markers.get(scope.checkpoint_namespace)
            if existing is not None:
                scope = replace(
                    scope,
                    scopes=tuple(existing["scopes"]),
                    grant_expires=existing["grant_expires"],
                )
                if scope.record() != existing:
                    raise PermissionError(
                        "Subagent invocation changed after admission"
                    )
                scope.effective_grant(invocation.scopes, expires)
            scope._validate(db, lease.owner, row, current_edges=True)
            if existing is None:
                markers[scope.checkpoint_namespace] = scope.record()
                db.execute(
                    "UPDATE runs SET state=? WHERE id=?",
                    (_json(state), lease.run_id),
                )
            return scope


@dataclass(frozen=True)
class SubagentGraphScope:
    """Private SDK graph or member activation beneath a child admission.

    The graph scheduler and each member have disjoint storage namespaces.
    Team members are pinned by the admitted child's entire revision list.
    """

    team: SubagentScope
    member_id: str | None = None
    activation_id: str | None = None

    @property
    def graph_namespace(self) -> str:
        return self.team.checkpoint_namespace + ":graph:team"

    @property
    def checkpoint_namespace(self) -> str:
        if self.activation_id is None:
            return self.graph_namespace
        return self.graph_namespace + ":member:" + self.activation_id

    @property
    def marker(self) -> list:
        return [self.team.marker, self.member_id]

    def native_call_id(self, call_id: str) -> str:
        return "child:" + _digest([self.checkpoint_namespace, call_id])

    def effective_grant(
        self, scopes: set[str] | frozenset[str], expires: int | None
    ) -> tuple[set[str], int | None]:
        return self.team.effective_grant(scopes, expires)

    def resolve_owned(
        self,
        db: sqlite3.Connection,
        owner: str,
        run_id: str,
        *,
        current_edges: bool = False,
    ) -> tuple[dict | None, ...]:
        from uuid import UUID

        chain = self.team.resolve_owned(
            db, owner, run_id, current_edges=current_edges
        )
        if not self.team.target_revisions:
            raise PermissionError("Team graph requires an admitted saved Agent")
        if self.member_id is None and self.activation_id is None:
            return chain
        if (
            not isinstance(self.activation_id, str)
            or str(UUID(self.activation_id)) != self.activation_id
            or self.member_id not in dict(self.team.target_revisions)
        ):
            raise PermissionError(
                "Team graph member is outside the admitted scope"
            )
        row = self.team._run(db, owner, run_id)
        state = json.loads(row["state"])
        if self.graph_namespace not in state.get("ownedGraphCheckpoints", {}):
            raise PermissionError("Team graph has no admitted checkpoint")
        marker = state.get("ownedSubagentLoopCheckpoints", {}).get(
            self.checkpoint_namespace
        )
        if marker is not None and marker != self.marker:
            raise PermissionError("Team graph activation identity changed")
        assert self.member_id is not None
        return (*chain, _agent(db, owner, self.member_id))

    def resolve(
        self, db: sqlite3.Connection, lease: ActorLease
    ) -> tuple[dict | None, ...]:
        lease.assert_current(db)
        return self.resolve_owned(
            db, lease.owner, lease.run_id, current_edges=True
        )
