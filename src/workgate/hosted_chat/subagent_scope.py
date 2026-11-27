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
    ) -> (
        GraphMemberScope
        | SubagentScope
        | SubagentGraphScope
        | DetachedExecutionScope
        | None
    ): ...


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
    parent_scope: (
        GraphMemberScope | SubagentScope | DetachedExecutionScope | None
    )
    parent_checkpoint_id: str
    call_id: str
    proposal_hash: str
    invocation_hash: str
    parent_revision: tuple[str, int] | None
    target_revisions: tuple[tuple[str, int], ...]
    remaining_depth: int
    scopes: tuple[str, ...]
    grant_expires: int | None
    team_definition_hash: str | None = None
    detached: bool = False

    def assert_team_root(self, root: dict) -> None:
        if (
            self.team_definition_hash is None
            or _digest(root) != self.team_definition_hash
        ):
            raise PermissionError(
                "Team graph definition does not match its admission"
            )

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
        if self.detached:
            raise PermissionError(
                "Detached child requires its own execution lease"
            )
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
                DetachedExecutionScope.from_record(parent)
                if "task_id" in parent
                else cls.from_record(parent)
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
        detached = detached_root_scope(self)
        if detached is not None:
            detached.resolve(db, lease)
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
        detached: bool = False,
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
            or (call.arguments.get("run_in_background") is True) != detached
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
        if detached and detached_root_scope(parent) is not None:
            raise PermissionError("Nested background delegation is not enabled")
        expires = invocation.grant_expires
        ceiling = (
            parent.admission
            if isinstance(parent, DetachedExecutionScope)
            else parent
        )
        if isinstance(ceiling, SubagentScope):
            if (
                invocation.remaining_depth >= ceiling.remaining_depth
                or not invocation.scopes.issubset(ceiling.scopes)
            ):
                raise PermissionError(
                    "Subagent child exceeds parent scope or nesting budget"
                )
            expires = min(
                (
                    value
                    for value in (expires, ceiling.grant_expires)
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
            if isinstance(parent, DetachedExecutionScope):
                parent.resolve(db, lease)
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
                _digest(invocation.agent)
                if invocation.agent.get("graphAgentIds")
                or invocation.agent.get("edges")
                else None,
                detached,
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

    team: SubagentScope | DetachedExecutionScope
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

        if isinstance(self.team, SubagentScope) and self.team.detached:
            raise PermissionError(
                "Detached team requires its own execution lease"
            )
        chain = (
            self.team.resolve_owned(
                db, owner, run_id, current_edges=current_edges
            )
            if isinstance(self.team, SubagentScope)
            else self.team.resolve_owned(db, owner, run_id)
        )
        if (
            not self.team.target_revisions
            or self.team.team_definition_hash is None
        ):
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
        detached = detached_root_scope(self)
        if detached is not None:
            detached.resolve(db, lease)
        return self.resolve_owned(
            db, lease.owner, lease.run_id, current_edges=True
        )


@dataclass(frozen=True)
class DetachedExecutionScope:
    """A private child actor bound to the original parent's durable admission."""

    task_id: str
    run_id: str
    authorization_conversation_id: str
    admission: SubagentScope
    disclosure_access: dict | None

    @property
    def target_revisions(self) -> tuple[tuple[str, int], ...]:
        return self.admission.target_revisions

    @property
    def team_definition_hash(self) -> str | None:
        return self.admission.team_definition_hash

    def assert_team_root(self, root: dict) -> None:
        self.admission.assert_team_root(root)

    def _run(self, db: sqlite3.Connection, owner: str, run_id: str):
        if run_id != self.run_id:
            raise PermissionError("Detached team belongs to another generation")
        row = db.execute(
            "SELECT r.state FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
            (run_id, owner),
        ).fetchone()
        if row is None:
            raise PermissionError("Detached team generation no longer owned")
        return row

    @property
    def member_id(self) -> str | None:
        return self.admission.member_id

    @property
    def checkpoint_namespace(self) -> str:
        return self.run_id + ":child:" + _digest(self.task_id)

    @property
    def marker(self) -> list:
        return [self.task_id, self.admission.marker]

    def native_call_id(self, call_id: str) -> str:
        return "child:" + _digest([self.checkpoint_namespace, call_id])

    def effective_grant(
        self, scopes: set[str] | frozenset[str], expires: int | None
    ) -> tuple[set[str], int | None]:
        return self.admission.effective_grant(scopes, expires)

    def cap_machine_access(self, policy: dict[str, str]) -> dict[str, str]:
        ceiling = (self.disclosure_access or {}).get("machines", {})
        rank = {"none": 0, "read": 1, "write": 2}
        return {
            machine: min(
                level, ceiling.get(machine, "none"), key=rank.__getitem__
            )
            for machine, level in policy.items()
        }

    @classmethod
    def from_record(cls, value: dict) -> DetachedExecutionScope:
        fields = dict(value)
        fields["admission"] = SubagentScope.from_record(fields["admission"])
        scope = cls(**fields)
        if json.loads(_json(asdict(scope))) != value:
            raise PermissionError("Invalid detached execution scope record")
        return scope

    @classmethod
    def load(
        cls, db: sqlite3.Connection, owner: str, run_id: str
    ) -> DetachedExecutionScope:
        row = db.execute(
            "SELECT t.*,r.state AS child_state,r.agent_id AS child_agent FROM subagent_tasks t "
            "JOIN runs r ON r.id=t.child_run_id JOIN conversations c ON c.id=r.conversation_id "
            "JOIN conversations p ON p.id=t.parent_conversation_id "
            "JOIN active_subagent_threads a ON a.owner=t.owner AND a.thread_id=c.id AND a.parent_run_id=t.parent_run_id "
            "WHERE t.child_run_id=? AND t.owner=? AND c.owner=t.owner AND p.owner=t.owner "
            "AND c.id=t.child_conversation_id AND t.child_registered=1 AND t.cancel_requested=0 "
            "AND t.status IN ('running','completed')",
            (run_id, owner),
        ).fetchone()
        if row is None:
            raise PermissionError("Detached child admission is unavailable")
        options = json.loads(row["execution_options"])
        admission = SubagentScope.from_record(options["scope"])
        if (
            not admission.detached
            or admission.run_id != row["parent_run_id"]
            or admission.checkpoint_namespace != row["idempotency_key"]
            or admission.invocation_hash != row["fingerprint"]
            or list(admission.scopes) != json.loads(row["granted_scopes"])
            or admission.grant_expires != row["grant_expires"]
            or admission.member_id != row["child_agent"]
            or json.loads(row["child_state"]).get("detachedTaskId") != row["id"]
        ):
            raise PermissionError("Detached child admission identity changed")
        return cls(
            row["id"],
            run_id,
            row["parent_conversation_id"],
            admission,
            options.get("disclosure_access"),
        )

    @classmethod
    def from_actor(cls, lease: ActorLease) -> DetachedExecutionScope:
        with lease.database.read_snapshot() as db:
            lease.assert_current(db)
            scope = cls.load(db, lease.owner, lease.run_id)
            scope.resolve(db, lease)
            return scope

    def resolve_owned(
        self, db: sqlite3.Connection, owner: str, run_id: str
    ) -> tuple[dict | None, ...]:
        if run_id != self.run_id or self.load(db, owner, run_id) != self:
            raise PermissionError("Detached child execution scope changed")
        # The parent may have advanced or finished. Its immutable admission
        # and all original Agent revisions still fence the detached child.
        return self.admission.resolve_owned(db, owner, self.admission.run_id)

    def resolve(
        self, db: sqlite3.Connection, lease: ActorLease
    ) -> tuple[dict | None, ...]:
        lease.assert_current(db)
        if lease.detached_task_id != self.task_id:
            raise PermissionError("Detached scope requires its task actor")
        return self.resolve_owned(db, lease.owner, lease.run_id)


def validate_detached_descendants(
    db: sqlite3.Connection,
    owner: str,
    task_id: str,
    child_run_id: str | None,
    scopes: set[str],
    expires: int | None,
) -> bool:
    """Fence any executed nested child's revisions before disclosing its result."""
    if child_run_id is None:
        row = db.execute(
            "SELECT execution_options FROM subagent_tasks WHERE id=? AND owner=?",
            (task_id, owner),
        ).fetchone()
        if row is None or json.loads(row["execution_options"]).get(
            "nested_provenance"
        ):
            raise PermissionError(
                "Detached descendant provenance no longer available"
            )
        return False
    detached = DetachedExecutionScope.load(db, owner, child_run_id)
    if detached.task_id != task_id:
        raise PermissionError("Detached descendant belongs to another task")
    row = detached._run(db, owner, child_run_id)
    records = json.loads(row["state"]).get("ownedSubagentScopes", {})
    for value in records.values():
        scope = SubagentScope.from_record(value)
        if detached_root_scope(scope) != detached or scope.detached:
            raise PermissionError("Detached descendant ancestry changed")
        scope.resolve_owned(db, owner, child_run_id)
        scope.effective_grant(scopes, expires)
    return bool(records)


def detached_root_scope(scope) -> DetachedExecutionScope | None:
    """Resolve the detached actor ceiling beneath a private descendant."""
    while isinstance(scope, (SubagentScope, SubagentGraphScope)):
        scope = (
            scope.parent_scope
            if isinstance(scope, SubagentScope)
            else scope.team
        )
    return scope if isinstance(scope, DetachedExecutionScope) else None
