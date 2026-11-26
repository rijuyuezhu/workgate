"""Machine-by-machine permission inheritance, scope fencing and SQLite persistence."""

import time

import pytest

from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.machine_permissions import (
    MachineReadOnlyError,
    access_level,
    agent_access_policy,
    authorize_machine_action,
    effective_machine_access,
    machine_access_context,
    require_machine_scope,
    resolve_access,
    validate_access_map,
)
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.protocol.ids import new_executor_id

LOCAL = "exec_" + "a" * 22
WORKER1 = "exec_" + "b" * 22
WORKER2 = "exec_" + "c" * 22
UNKNOWN = "exec_" + "d" * 22
ALL = {LOCAL, WORKER1, WORKER2}
SCOPES = {"shell:read", "shell:write", "shell:execute"}


def test_effective_permission_precedence_and_isolation(tmp_path):
    repo = WorkspaceRepository(SQLiteDatabase(tmp_path / "state"))
    project = repo.create_project("alice", "Research")
    pid = project["id"]
    chat = repo.create_conversation(
        "alice", {"title": "Message", "projectId": pid}
    )
    cid = chat["conversationId"]
    repo.save_setting(
        "alice",
        "agent.machine_policy.global",
        {LOCAL: "read", WORKER1: "write"},
    )
    repo.save_setting(
        "alice",
        "agent.machine_policy.project." + pid,
        {LOCAL: "write", WORKER1: "none", WORKER2: "read"},
    )
    repo.save_setting(
        "alice",
        "agent.machine_policy.conversation." + cid,
        {LOCAL: "none", WORKER2: "write"},
    )
    assert resolve_access("alice", cid, repo=repo, registered=ALL) == {
        LOCAL: "none",
        WORKER1: "none",
        WORKER2: "write",
    }
    assert (
        access_level("alice", cid, WORKER2, repo=repo, registered=ALL)
        == "write"
    )
    assert (
        access_level("alice", cid, UNKNOWN, repo=repo, registered=ALL) == "none"
    )
    assert (
        resolve_access("alice", project_id=pid, repo=repo, registered=ALL)[
            LOCAL
        ]
        == "write"
    )
    plain = repo.create_conversation("alice", {"title": "No project"})
    assert (
        access_level(
            "alice", plain["conversationId"], LOCAL, repo=repo, registered=ALL
        )
        == "read"
    )
    assert resolve_access("bob", repo=repo, registered=ALL)[LOCAL] == "write"
    with pytest.raises(KeyError):
        resolve_access("bob", cid, repo=repo, registered=ALL)
    with pytest.raises(KeyError):
        resolve_access("alice", project_id="unknown", repo=repo, registered=ALL)
    # Moving or unassigning the conversation uses its actual current parent.
    another = repo.create_project("alice", "Other")["id"]
    repo.save_setting(
        "alice", "agent.machine_policy.project." + another, {WORKER1: "read"}
    )
    repo.update_conversation("alice", cid, {"projectId": another})
    assert (
        resolve_access("alice", cid, project_id=pid, repo=repo, registered=ALL)[
            WORKER1
        ]
        == "read"
    )
    repo.update_conversation("alice", cid, {"projectId": None})
    assert (
        resolve_access("alice", cid, repo=repo, registered=ALL)[WORKER1]
        == "write"
    )


@pytest.mark.parametrize(
    "value",
    [
        [],
        {LOCAL: "admin"},
        {"*": "read"},
        {LOCAL: True},
        {"../../../": "read"},
        {"": "read"},
        {"local": "read"},
        {LOCAL: None},
        {LOCAL: []},
        {new_executor_id(): "read" for _ in range(129)},
    ],
)
def test_invalid_machine_maps_fail_closed(value):
    with pytest.raises(ValueError):
        validate_access_map(value)


def test_newly_registered_executors_default_to_write_and_explicit_denials_win(
    tmp_path,
):
    registered = {LOCAL, WORKER1}
    repo = WorkspaceRepository(SQLiteDatabase(tmp_path / "default-all"))
    project = repo.create_project("alice", "Default project")["id"]
    cid = repo.create_conversation(
        "alice", {"title": "Default chat", "projectId": project}
    )["conversationId"]
    assert resolve_access("alice", cid, repo=repo, registered=registered) == {
        LOCAL: "write",
        WORKER1: "write",
    }
    registered.add(WORKER2)
    assert (
        access_level("alice", cid, WORKER2, repo=repo, registered=registered)
        == "write"
    )
    assert (
        access_level("alice", cid, UNKNOWN, repo=repo, registered=registered)
        == "none"
    )
    repo.save_setting(
        "alice",
        "agent.machine_policy.global",
        {WORKER1: "none", UNKNOWN: "write"},
    )
    repo.save_setting(
        "alice", "agent.machine_policy.project." + project, {LOCAL: "read"}
    )
    repo.save_setting(
        "alice", "agent.machine_policy.conversation." + cid, {WORKER2: "none"}
    )
    assert resolve_access("alice", cid, repo=repo, registered=registered) == {
        LOCAL: "read",
        WORKER1: "none",
        WORKER2: "none",
    }
    registered.remove(WORKER2)
    assert (
        access_level("alice", cid, WORKER2, repo=repo, registered=registered)
        == "none"
    )
    assert resolve_access("alice", cid, repo=repo, registered=registered) == {
        LOCAL: "read",
        WORKER1: "none",
    }


def test_scopes_are_checked_per_operation():
    with pytest.raises(PermissionError, match="shell:read"):
        require_machine_scope(set(), "read")
    with pytest.raises(PermissionError, match="shell:write"):
        require_machine_scope({"shell:read"}, "write")
    with pytest.raises(PermissionError, match="shell:write"):
        require_machine_scope({"shell:read", "shell:execute"}, "execute")
    with pytest.raises(PermissionError, match="shell:execute"):
        require_machine_scope({"shell:read", "shell:write"}, "execute")
    with pytest.raises(PermissionError, match="expired"):
        require_machine_scope({"shell:read"}, "read", expires=1)
    with pytest.raises(ValueError, match="Unknown machine operation"):
        require_machine_scope(SCOPES, "unknown")
    require_machine_scope({"shell:read"}, "read")
    with pytest.raises(PermissionError, match="shell:read"):
        require_machine_scope({"shell:write"}, "write")
    require_machine_scope({"shell:read", "shell:write"}, "write")
    require_machine_scope(SCOPES, "execute", expires=int(time.time()) + 60)


def test_live_agent_ceiling_context_and_revocation(tmp_path):
    repo = WorkspaceRepository(SQLiteDatabase(tmp_path))
    cid = repo.create_conversation("alice", {})["conversationId"]
    kwargs = {"repo": repo, "registered": ALL}
    agent = {
        "machine_default_permission": "read",
        "machine_permissions": {LOCAL: "write", WORKER1: "none"},
    }
    assert agent_access_policy({}) is None
    assert agent_access_policy(agent) == (agent["machine_permissions"], "read")
    assert effective_machine_access(
        "alice", cid, agent=agent, scopes=SCOPES, **kwargs
    ) == ({LOCAL: "write", WORKER1: "none", WORKER2: "read"}, "read")
    assert effective_machine_access(
        "alice", cid, agent=agent, scopes={"shell:read"}, **kwargs
    )[0] == {LOCAL: "read", WORKER1: "none", WORKER2: "read"}
    assert effective_machine_access(
        "alice", cid, agent=agent, scopes=SCOPES, expires=1, **kwargs
    )[0] == dict.fromkeys(ALL, "none")
    for target, operation in [(LOCAL, "execute"), (WORKER2, "read")]:
        authorize_machine_action(
            "alice",
            cid,
            target,
            operation,
            agent=agent,
            scopes=SCOPES,
            **kwargs,
        )
    with pytest.raises(MachineReadOnlyError):
        authorize_machine_action(
            "alice",
            cid,
            WORKER2,
            "execute",
            agent=agent,
            scopes=SCOPES,
            **kwargs,
        )
    for target in (WORKER1, UNKNOWN, "local"):
        with pytest.raises(PermissionError):
            authorize_machine_action(
                "alice",
                cid,
                target,
                "read",
                agent=agent,
                scopes=SCOPES,
                **kwargs,
            )
    repo.save_setting(
        "alice", "agent.machine_policy.conversation." + cid, {LOCAL: "read"}
    )
    with pytest.raises(MachineReadOnlyError):
        authorize_machine_action(
            "alice", cid, LOCAL, "write", agent=agent, scopes=SCOPES, **kwargs
        )
    context = machine_access_context(
        "alice", cid, agent=agent, scopes=SCOPES, **kwargs
    )
    assert (
        "Read-only" in context
        and "No access" in context
        and "Agent default" in context
    )
    assert LOCAL in context and WORKER1 in context and WORKER2 in context
    with repo.database.read_snapshot() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)
            ).fetchone()[0]
            == 0
        )
    other = repo.create_conversation("alice", {})["conversationId"]
    assert (
        machine_access_context(
            "alice", other, agent=None, scopes=SCOPES, **kwargs
        )
        == ""
    )
    assert "execution-class" in machine_access_context(
        "alice",
        other,
        agent=None,
        scopes={"shell:read", "shell:write"},
        **kwargs,
    )
    assert "No registered Executors" in machine_access_context(
        "alice", other, agent=None, scopes=SCOPES, repo=repo, registered=set()
    )
    with pytest.raises(ValueError):
        agent_access_policy({"machine_default_permission": []})
    repo.save_setting(
        "alice", "agent.machine_policy.global", {LOCAL: "invalid"}
    )
    with pytest.raises(ValueError):
        authorize_machine_action(
            "alice", cid, LOCAL, "read", agent=None, scopes=SCOPES, **kwargs
        )
