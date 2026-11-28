"""Owner-scoped per-machine Agent permissions with inherited SQLite defaults.

Source: upstream v6.0.3, webui/machine_permissions.py
(466d2103bb2c3a7da90438663751443517761121). See LICENSE.upstream.
Registered trusted Executors default to write; no implicit Controller target.
Native dispatch must still enforce the live trust, routing and tool scopes.
"""

import json
import time
from collections.abc import Collection, Sequence
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..oauth.core.scopes import (
    SCOPE_SHELL_EXECUTE,
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from ..protocol.ids import ExecutorId
from .storage import WorkspaceRepository

_LEVELS = frozenset({"none", "read", "write"})
_EXECUTOR_ID = TypeAdapter(ExecutorId)
_GLOBAL = "agent.machine_policy.global"
_PROJECT = "agent.machine_policy.project."
_CONVERSATION = "agent.machine_policy.conversation."


class MachineReadOnlyError(PermissionError):
    """The requested operation exceeds a read-only machine grant."""


def _validate_executor_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid Executor identifier")
    try:
        return _EXECUTOR_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise ValueError("Invalid Executor identifier") from exc


def validate_access_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError(
            "Machine permissions must be an object with at most 128 entries"
        )
    result: dict[str, str] = {}
    for name, access in value.items():
        _validate_executor_id(name)
        if not isinstance(access, str) or access not in _LEVELS:
            raise ValueError("Machine access must be none, read or write")
        result[name] = access
    return result


def agent_access_policy(agent: dict) -> tuple[dict[str, str], str] | None:
    """Resolve an Agent's ACL, preserving legacy per-tool and implicit-write policies."""
    if (
        "machine_permissions" not in agent
        and "machine_default_permission" not in agent
    ):
        return None
    default = agent.get("machine_default_permission", "write")
    if not isinstance(default, str) or default not in _LEVELS:
        raise ValueError("Default machine access must be none, read or write")
    return validate_access_map(agent.get("machine_permissions", {})), default


def _stored(db, owner: str, key: str) -> dict[str, str]:
    row = db.execute(
        "SELECT value FROM settings WHERE owner=? AND key=?", (owner, key)
    ).fetchone()
    # Corruption must not silently widen access to the implicit write default.
    return validate_access_map(json.loads(row[0])) if row else {}


def resolve_access(
    owner: str,
    conversation_id: str | None = None,
    *,
    project_id: str | None = None,
    repo: WorkspaceRepository,
    registered: Collection[str],
) -> dict[str, str]:
    """Resolve preferences in one SQLite snapshot for currently trusted IDs.

    The native Control supplies its live trust snapshot, including offline
    Executors but excluding revoked ones. Stored overrides cannot create targets.
    """
    identities = tuple(
        _validate_executor_id(identity) for identity in registered
    )
    with repo.database.read_snapshot() as db:
        return _resolve_access(
            db, owner, conversation_id, project_id, identities
        )


def _resolve_access(db, owner, conversation_id, project_id, identities):
    policy = dict.fromkeys(identities, "write")
    policy.update(_stored(db, owner, _GLOBAL))
    if conversation_id is not None:
        convo = db.execute(
            "SELECT project_id FROM conversations WHERE id=? AND owner=?",
            (conversation_id, owner),
        ).fetchone()
        if convo is None:
            raise KeyError("Conversation not found")
        project_id = convo["project_id"]
    if project_id is not None:
        if not db.execute(
            "SELECT 1 FROM projects WHERE id=? AND owner=?",
            (project_id, owner),
        ).fetchone():
            raise KeyError("Project not found")
        policy.update(_stored(db, owner, _PROJECT + project_id))
    if conversation_id is not None:
        policy.update(_stored(db, owner, _CONVERSATION + conversation_id))
    return {identity: policy[identity] for identity in identities}


def machine_policy(
    owner: str,
    *,
    repo: WorkspaceRepository,
    registered: Collection[str],
    conversation_id: str | None = None,
    project_id: str | None = None,
    changes: dict[str, str] | None = None,
) -> dict:
    """Read or replace a single owner policy in an ownership-fenced transaction."""
    if conversation_id is not None and project_id is not None:
        raise ValueError("Choose one policy target")
    identities = tuple(
        _validate_executor_id(identity) for identity in registered
    )
    key = (
        _CONVERSATION + conversation_id
        if conversation_id is not None
        else _PROJECT + project_id
        if project_id is not None
        else _GLOBAL
    )
    with repo.database.connect() as db:
        db.execute("BEGIN IMMEDIATE" if changes is not None else "BEGIN")
        effective = _resolve_access(
            db, owner, conversation_id, project_id, identities
        )
        if changes is not None:
            value = validate_access_map(changes)
            if not value.keys() <= set(identities):
                raise ValueError("Unregistered Executor in policy")
            db.execute(
                "INSERT INTO settings(owner,key,value) VALUES(?,?,?) "
                "ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value",
                (owner, key, json.dumps(value, separators=(",", ":"))),
            )
            effective = _resolve_access(
                db, owner, conversation_id, project_id, identities
            )
        else:
            value = _stored(db, owner, key)
    return {"machines": value, "effective": effective}


_RANK = {"none": 0, "read": 1, "write": 2}


def _agent_ceiling(
    policy: dict[str, str], agent: dict | None
) -> tuple[dict[str, str], str]:
    agent_policy, default = agent_access_policy(agent or {}) or ({}, "write")
    return {
        name: min(level, agent_policy.get(name, default), key=_RANK.__getitem__)
        for name, level in policy.items()
    }, default


def _agent_chain_ceiling(
    policy: dict[str, str], agent: dict | None, ancestors: Sequence[dict | None]
) -> tuple[dict[str, str], str]:
    policy, default = _agent_ceiling(policy, agent)
    for parent in ancestors:
        policy, parent_default = _agent_ceiling(policy, parent)
        default = min(default, parent_default, key=_RANK.__getitem__)
    return policy, default


def effective_machine_access(
    owner: str,
    conversation_id: str,
    *,
    agent: dict | None,
    scopes: set[str],
    expires: int | None = None,
    repo: WorkspaceRepository,
    registered: Collection[str],
    ancestors: Sequence[dict | None] = (),
) -> tuple[dict[str, str], str]:
    """Resolve the live Executor ACL subject to the Agent and principal ceilings."""
    policy, default = _agent_chain_ceiling(
        resolve_access(
            owner, conversation_id, repo=repo, registered=registered
        ),
        agent,
        ancestors,
    )
    effective: dict[str, str] = {}
    for name, level in sorted(policy.items()):
        if level != "none":
            try:
                require_machine_scope(scopes, "read", expires=expires)
            except PermissionError:
                level = "none"
            if level == "write":
                try:
                    require_machine_scope(scopes, "write", expires=expires)
                except PermissionError:
                    level = "read"
        effective[name] = level
    return effective, default


def machine_access_context(
    owner: str,
    conversation_id: str,
    *,
    agent: dict | None,
    scopes: set[str],
    expires: int | None = None,
    repo: WorkspaceRepository,
    registered: Collection[str],
) -> str:
    """Render the live native-machine ACL, not a grant or a history message."""
    effective, default = effective_machine_access(
        owner,
        conversation_id,
        agent=agent,
        scopes=scopes,
        expires=expires,
        repo=repo,
        registered=registered,
    )
    if not effective:
        return "No registered Executors are currently authorized. Do not invent a local execution target."
    groups: dict[str, list[str]] = {"none": [], "read": [], "write": []}
    for name, level in effective.items():
        groups[level].append(name)
    execution_restricted = SCOPE_SHELL_EXECUTE not in scopes and bool(
        groups["write"]
    )
    if not (
        groups["none"]
        or groups["read"]
        or default != "write"
        or execution_restricted
    ):
        return ""
    lines = ["Current native Executor access restrictions (server-enforced):"]
    for level, label in (
        ("none", "No access"),
        ("read", "Read-only"),
        ("write", "Read/write"),
    ):
        if groups[level]:
            lines.append(f"- {label}: {', '.join(groups[level])}")
    if default != "write":
        label = {"none": "no access", "read": "read-only"}[default]
        lines.append(f"- Agent default for newly registered Executors: {label}")
    if execution_restricted:
        lines.append(
            "- Shell and other execution-class operations are not authorized."
        )
    lines.append(
        "Unlisted, unregistered Executors are not implicitly authorized. "
        "Use read-only inspection tools on read-only machines; do not run shell "
        "commands, scripts, or other mutating operations on them. "
        "Other tool-specific restrictions still apply."
    )
    return "\n".join(lines)


def access_level(
    owner: str,
    conversation_id: str,
    machine: str,
    *,
    repo: WorkspaceRepository,
    registered: Collection[str],
) -> str:
    return resolve_access(
        owner, conversation_id, repo=repo, registered=registered
    ).get(machine, "none")


def require_machine_scope(
    scopes: set[str], operation: str, *, expires: int | None = None
) -> None:
    if operation not in {"read", "write", "execute"}:
        raise ValueError("Unknown machine operation class")
    if expires is not None and (
        type(expires) is not int or expires <= time.time()
    ):
        raise PermissionError("Agent session authorization has expired")
    required_scopes = [SCOPE_SHELL_READ]
    if operation != "read":
        required_scopes.append(SCOPE_SHELL_WRITE)
    if operation == "execute":
        required_scopes.append(SCOPE_SHELL_EXECUTE)
    for required in required_scopes:
        if required not in scopes:
            raise PermissionError(f"{required} permission is required")


def authorize_machine_action(
    owner: str,
    conversation_id: str,
    machine: str,
    operation: str,
    *,
    agent: dict | None,
    scopes: set[str],
    expires: int | None = None,
    repo: WorkspaceRepository,
    registered: Collection[str],
    ancestors: Sequence[dict | None] = (),
) -> None:
    """Pre-dispatch ACL gate; the native dispatcher still checks tool scopes.

    Execute requires write authority regardless of shell command text.
    """
    if operation not in {"read", "write", "execute"}:
        raise ValueError("Unknown machine operation class")
    try:
        _validate_executor_id(machine)
    except ValueError as exc:
        raise PermissionError("Invalid target Executor") from exc
    policy, _ = _agent_chain_ceiling(
        resolve_access(
            owner, conversation_id, repo=repo, registered=registered
        ),
        agent,
        ancestors,
    )
    granted = policy.get(machine, "none")
    if granted == "none":
        raise PermissionError(f"Access to Executor {machine} is not granted")
    if operation != "read" and granted != "write":
        raise MachineReadOnlyError(f"Executor {machine} is read-only")
    require_machine_scope(scopes, operation, expires=expires)
