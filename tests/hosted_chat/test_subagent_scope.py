"""Foreground delegation recovery, isolation and live authority."""

import json
import time
from dataclasses import replace
from typing import cast

import pytest

from workgate.hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    RuntimeSafetyError,
)
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.checkpoints import SQLiteCheckpointSaver
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig
from workgate.hosted_chat.subagent_scope import SubagentScope
from workgate.hosted_chat.subagents import (
    build_subagent_configs,
    chat_subagent_config,
    invoke_subagent,
)


def agent(db, **body):
    return AgentRepository(db).create(
        "alice", {"name": "Agent", "provider": "openai", "model": None, **body}
    )


def context(agent):
    return AgentContext(
        ProviderConfig("openai", "model", "https://provider.invalid", "secret"),
        [],
        agent=agent,
    )


@pytest.fixture
def setup(tmp_path):
    db = SQLiteDatabase(tmp_path)
    leaf = agent(db)
    child = agent(db, subagents={"enabled": True, "agent_ids": [leaf["id"]]})
    parent = agent(db, subagents={"enabled": True, "agent_ids": [child["id"]]})
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id,owner) VALUES('chat','alice')"
        )
        conn.execute(
            "INSERT INTO runs(id,conversation_id,status,agent_id) VALUES('run','chat','running',?)",
            (parent["id"],),
        )
    lease = ActorLease.claim(db, "alice", "run")
    assert lease is not None
    return lease, parent, child, leaf


async def edge(
    checkpoint, source, target, *, call_id="same", description="child task"
):
    call = ModelToolCall(
        call_id,
        "subagent",
        {"subagent_type": target, "description": description},
    )
    state = AgentLoopState(
        context(source),
        [{"role": "user", "content": "parent private"}],
        step=1,
        pending_calls=[call],
        tool_step_started=True,
    )
    await checkpoint.save(state, [call])
    return call


async def invocation(
    checkpoint, source, call, *, configs=None, max_depth=5, scopes=None
):
    captured = []

    async def execute(value):
        captured.append(value)
        return "done"

    lease = checkpoint.lease
    if configs is None:
        configs = (
            build_subagent_configs(lease.owner, source, lease.database)
            if source
            else [chat_subagent_config(lease.owner)]
        )
    result = await invoke_subagent(
        call,
        configs,
        execute_child=execute,
        current_grant=lambda: (
            scopes if scopes is not None else {"shell:read"},
            int(time.time()) + 600,
        ),
        owner=lease.owner,
        conversation_id="chat",
        parent_run_id=lease.run_id,
        database=lease.database,
        parent_agent_id=source["id"] if source else None,
        max_depth=max_depth,
    )
    assert result == "done"
    return captured[0]


@pytest.mark.asyncio
async def test_foreground_child_recovery_and_private_saver(setup):
    lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"])
    value = await invocation(root, parent, call)
    scope = SubagentScope.admit(root, call, value)
    assert scope.member_id == child["id"]
    owned = OwnedLoopCheckpoint(lease, scope=scope)
    assert owned.restore(context(child)) is None
    with pytest.raises(RuntimeSafetyError, match="context"):
        owned.restore(context(parent))
    child_state = AgentLoopState(
        context(child),
        value.messages + [{"role": "assistant", "content": "private answer"}],
        completed=True,
    )
    await owned.save(child_state, [])
    parent_recovery = root.restore(context(parent))
    assert parent_recovery is not None
    assert parent_recovery.state.pending_calls == [call]
    for saver in (
        root.saver,
        SQLiteCheckpointSaver(lease.database, lease.owner),
    ):
        with pytest.raises(ActorLeaseLost):
            saver.get_tuple(owned.base)
        with pytest.raises(ActorLeaseLost):
            list(saver.list(owned.base))
    with pytest.raises(ActorLeaseLost):
        owned.saver.get_tuple(root.base)
    with pytest.raises(ActorLeaseLost):
        list(owned.saver.list(root.base))
    assert len(list(owned.saver.list(owned.base))) == 1
    with pytest.raises(ActorLeaseLost):
        root.saver.put_writes(owned.active, [("receipt", "bad")], "fake")
    lease.release()
    fresh = ActorLease.claim(lease.database, lease.owner, lease.run_id)
    assert fresh is not None
    with pytest.raises(ActorLeaseLost):
        owned.restore(context(child))
    resumed = OwnedLoopCheckpoint(fresh)
    assert resumed.restore(context(parent)) is not None
    assert value.grant_expires is not None
    expanded = replace(
        value,
        scopes=frozenset({"shell:read", "shell:write"}),
        grant_expires=value.grant_expires + 1000,
    )
    assert SubagentScope.admit(resumed, call, expanded) == scope
    assert scope.effective_grant(
        set(expanded.scopes), expanded.grant_expires
    ) == ({"shell:read"}, value.grant_expires)
    with pytest.raises(PermissionError, match="reduced"):
        SubagentScope.admit(resumed, call, replace(value, scopes=frozenset()))
    recovered = OwnedLoopCheckpoint(fresh, scope=scope).restore(context(child))
    assert recovered is not None
    assert (
        recovered.state.completed
        and recovered.state.messages == child_state.messages
    )
    assert "parent private" not in str(recovered.state.messages)
    with fresh.database.connect() as db:
        db.execute(
            "DELETE FROM agent_checkpoints WHERE checkpoint_ns=?",
            (scope.checkpoint_namespace,),
        )
    with pytest.raises(RuntimeSafetyError, match="no longer available"):
        OwnedLoopCheckpoint(fresh, scope=scope).restore(context(child))


@pytest.mark.asyncio
async def test_exact_edge_and_revisions(setup):
    lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"])
    value = await invocation(root, parent, call)
    for bad in (
        replace(value, owner="bob"),
        replace(value, parent_run_id="other"),
        replace(value, messages=[{"role": "user", "content": "wrong"}]),
        replace(value, grant_expires=1),
    ):
        with pytest.raises(PermissionError):
            SubagentScope.admit(root, call, bad)
    wrong = replace(call, arguments={**call.arguments, "intent": "altered"})
    with pytest.raises(PermissionError, match="proposal"):
        SubagentScope.admit(root, wrong, value)
    scope = SubagentScope.admit(root, call, value)
    with pytest.raises(PermissionError, match="changed after admission"):
        SubagentScope.admit(root, call, replace(value, max_turns=1))
    new_call = await edge(root, parent, child["id"])
    second = SubagentScope.admit(root, new_call, value)
    assert second.checkpoint_namespace != scope.checkpoint_namespace
    assert second.native_call_id("effect") != scope.native_call_id("effect")
    with pytest.raises(PermissionError, match="no longer current"):
        OwnedLoopCheckpoint(lease, scope=scope)
    AgentRepository(lease.database).update(
        "alice", child["id"], {"name": "edited"}
    )
    with pytest.raises(PermissionError, match="target revision changed"):
        OwnedLoopCheckpoint(lease, scope=second)


@pytest.mark.asyncio
async def test_nested_children_cannot_expand_parent_authority(setup):
    lease, parent, child, leaf = setup
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"])
    value = await invocation(root, parent, call)
    scope = SubagentScope.admit(root, call, value)
    child_checkpoint = OwnedLoopCheckpoint(lease, scope=scope)
    nested_call = await edge(child_checkpoint, child, leaf["id"])
    nested = await invocation(
        child_checkpoint,
        child,
        nested_call,
        configs=list(value.child_configs),
        max_depth=scope.remaining_depth,
    )
    for bad in (
        replace(nested, remaining_depth=scope.remaining_depth),
        replace(nested, scopes=frozenset({"shell:write"})),
    ):
        with pytest.raises(PermissionError, match="parent scope or nesting"):
            SubagentScope.admit(child_checkpoint, nested_call, bad)
    nested_scope = SubagentScope.admit(child_checkpoint, nested_call, nested)
    with lease.database.read_snapshot() as db:
        chain = nested_scope.resolve(db, lease)
        assert all(item is not None for item in chain)
        assert [item["id"] for item in chain if item is not None] == [
            parent["id"],
            child["id"],
            leaf["id"],
        ]
    assert nested_scope.native_call_id("same") != scope.native_call_id("same")
    await edge(root, parent, child["id"])
    with pytest.raises(PermissionError, match="no longer current"):
        OwnedLoopCheckpoint(lease, scope=nested_scope)


@pytest.mark.asyncio
async def test_ordinary_chat_child_and_forged_admission(tmp_path):
    db = SQLiteDatabase(tmp_path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id,owner) VALUES('chat','alice')"
        )
        conn.execute(
            "INSERT INTO runs(id,conversation_id,status) VALUES('run','chat','running')"
        )
    lease = ActorLease.claim(db, "alice", "run")
    assert lease is not None
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, None, "self")
    value = await invocation(root, None, call)
    scope = SubagentScope.admit(root, call, value)
    assert scope.member_id is None
    with pytest.raises(PermissionError, match="no matching admission"):
        OwnedLoopCheckpoint(
            lease, scope=replace(scope, invocation_hash="forged")
        )
    with db.read_snapshot() as conn:
        assert scope.resolve(conn, lease) == (None, None)
    checkpoint = OwnedLoopCheckpoint(lease, scope=scope)
    assert checkpoint.restore(context(None)) is None
    await checkpoint.save(
        AgentLoopState(context(None), value.messages, completed=True), []
    )
    recovered = checkpoint.restore(context(None))
    assert recovered is not None and recovered.state.completed


@pytest.mark.asyncio
async def test_graph_member_parent_and_live_revision_chain(setup):
    from uuid import uuid4

    from workgate.hosted_chat.graph_member_scope import GraphMemberScope

    lease, parent, child, leaf = setup
    repo = AgentRepository(lease.database)
    root = repo.update(
        "alice",
        parent["id"],
        {
            "edges": [
                {"from": parent["id"], "to": child["id"], "edgeType": "direct"}
            ]
        },
    )
    assert root is not None
    graph_namespace = "run:graph:test"
    with lease.database.connect() as db:
        db.execute(
            "UPDATE runs SET state=json_set(state,'$.ownedGraphCheckpoints',json(?)) WHERE id='run'",
            ('{"run:graph:test":true}',),
        )
    member = GraphMemberScope(
        graph_namespace, root["id"], root["version"], child["id"], str(uuid4())
    )
    checkpoint = OwnedLoopCheckpoint(lease, scope=member)
    call = await edge(checkpoint, child, leaf["id"])
    value = await invocation(checkpoint, child, call)
    scope = SubagentScope.admit(checkpoint, call, value)
    with lease.database.read_snapshot() as db:
        chain = scope.resolve(db, lease)
        assert [item["id"] for item in chain if item is not None] == [
            root["id"],
            child["id"],
            leaf["id"],
        ]
    assert (
        OwnedLoopCheckpoint(lease, scope=scope).restore(context(leaf)) is None
    )
    repo.update("alice", root["id"], {"name": "changed"})
    with pytest.raises(PermissionError, match="membership changed"):
        OwnedLoopCheckpoint(lease, scope=scope)


@pytest.mark.asyncio
async def test_team_scope_pins_all_member_revisions(setup):
    lease, parent, child, leaf = setup
    repo = AgentRepository(lease.database)
    team = {
        "type": "team",
        "name": "Team",
        "description": "Review",
        "agent_ids": [child["id"], leaf["id"]],
        "entry_agent_id": child["id"],
        "result_agent_id": leaf["id"],
        "edges": [
            {"from": child["id"], "to": leaf["id"], "edgeType": "direct"}
        ],
    }
    parent = repo.update(
        "alice",
        parent["id"],
        {"subagents": {"enabled": True, "allowSelf": False, "graphs": [team]}},
    )
    assert parent is not None
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, "team")
    value = await invocation(root, parent, call)
    scope = SubagentScope.admit(root, call, value)
    assert scope.target_revisions == (
        (child["id"], child["version"]),
        (leaf["id"], leaf["version"]),
    )
    repo.update("alice", leaf["id"], {"instructions": "changed"})
    with pytest.raises(PermissionError, match="target revision changed"):
        OwnedLoopCheckpoint(lease, scope=scope)


@pytest.mark.asyncio
async def test_native_child_scope_caps_grant_and_fences_historical_cleanup(
    setup,
):
    from workgate.hosted_chat.durable_tasks import DurableTaskRepository
    from workgate.protocol.ids import new_executor_id

    lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(lease)
    machine = new_executor_id()
    registered = (machine,)
    read = {"shell:read"}
    write = read | {"shell:write", "shell:execute"}
    task = ModelToolCall("same", "task", {"action": "create"})
    call = await edge(root, parent, child["id"])
    value = await invocation(root, parent, call)
    read_scope = SubagentScope.admit(root, call, value)
    child_repo = DurableTaskRepository(lease.database, scope=read_scope)
    assert child_repo.cap_grant(write, None) == (read, value.grant_expires)
    with pytest.raises(PermissionError):
        child_repo.reserve(
            lease,
            task,
            machine=machine,
            scopes=write,
            expires=None,
            registered=registered,
        )
    with pytest.raises(PermissionError, match="reduced"):
        child_repo.cap_grant(set(), None)
    call = await edge(root, parent, child["id"])
    value = await invocation(root, parent, call, scopes=write)
    scope = SubagentScope.admit(root, call, value)
    repo = DurableTaskRepository(lease.database, scope=scope)
    assert SubagentScope.from_record(scope.record()) == scope
    assert [a["id"] for a in repo.tool_context_chain(lease)[1] if a] == [
        parent["id"],
        child["id"],
    ]
    root_repo = DurableTaskRepository(lease.database)
    direct = root_repo.reserve(
        lease,
        task,
        machine=machine,
        scopes=write,
        expires=None,
        registered=registered,
    )
    intent = repo.reserve(
        lease,
        task,
        machine=machine,
        scopes=write,
        expires=None,
        registered=registered,
    )
    assert intent.id != direct.id
    with pytest.raises(PermissionError):
        root_repo.claim(
            lease,
            intent,
            task,
            scopes=write,
            expires=None,
            registered=registered,
        )
    assert repo.claim(
        lease, intent, task, scopes=write, expires=None, registered=registered
    )
    active = repo.bind(lease, intent, intent.key)
    assert active.state == "active"
    with lease.database.read_snapshot() as db:
        row = db.execute(
            "SELECT call_id,member_namespace,grant_expires FROM agent_durable_tasks WHERE id=?",
            (intent.id,),
        ).fetchone()
        assert row["call_id"] == scope.native_call_id(task.call_id)
        assert row["member_namespace"] == scope.checkpoint_namespace
        assert row["grant_expires"] == scope.grant_expires
    # A completed parent edge is not itself authorization revocation.
    await edge(root, parent, child["id"])
    with pytest.raises(PermissionError, match="no longer current"):
        repo.tool_context_chain(lease)
    assert root_repo.cancellation_batch(registered) == []
    AgentRepository(lease.database).update(
        "alice", child["id"], {"machine_default_permission": "none"}
    )
    with lease.database.connect() as db:
        db.execute("UPDATE agent_durable_tasks SET next_attempt_at=NULL")
    batch = root_repo.cancellation_batch(registered)
    assert [item["id"] for item in batch] == [intent.id]


@pytest.mark.asyncio
async def test_native_nested_scope_intersects_all_ancestor_acl(setup):
    from workgate.hosted_chat.durable_tasks import DurableTaskRepository
    from workgate.protocol.ids import new_executor_id

    lease, parent, child, leaf = setup
    repo = AgentRepository(lease.database)
    machine = new_executor_id()
    parent = repo.update(
        "alice", parent["id"], {"machine_permissions": {machine: "read"}}
    )
    assert parent is not None
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"])
    scopes = {"shell:read", "shell:write", "shell:execute"}
    value = await invocation(root, parent, call, scopes=scopes)
    scope = SubagentScope.admit(root, call, value)
    child_checkpoint = OwnedLoopCheckpoint(lease, scope=scope)
    nested_call = await edge(child_checkpoint, child, leaf["id"])
    nested = await invocation(
        child_checkpoint,
        child,
        nested_call,
        configs=list(value.child_configs),
        max_depth=scope.remaining_depth,
        scopes=scopes,
    )
    nested_scope = SubagentScope.admit(child_checkpoint, nested_call, nested)
    ledger = DurableTaskRepository(lease.database, scope=nested_scope)
    assert SubagentScope.from_record(nested_scope.record()) == nested_scope
    assert [a["id"] for a in ledger.tool_context_chain(lease)[1] if a] == [
        parent["id"],
        child["id"],
        leaf["id"],
    ]
    task = ModelToolCall("call", "task", {"action": "create"})
    task_intent = ledger.reserve(
        lease,
        task,
        machine=machine,
        scopes=scopes,
        expires=None,
        registered=(machine,),
    )
    assert ledger.claim(
        lease,
        task_intent,
        task,
        scopes=scopes,
        expires=None,
        registered=(machine,),
    )
    task_id = ledger.bind(lease, task_intent, task_intent.key).key
    session = ModelToolCall(
        "session", "session_start", {"executor_id": machine, "task_id": task_id}
    )
    session_intent = ledger.reserve(
        lease,
        session,
        machine=machine,
        scopes=scopes,
        expires=None,
        registered=(machine,),
    )
    assert ledger.claim(
        lease,
        session_intent,
        session,
        scopes=scopes,
        expires=None,
        registered=(machine,),
    )
    session_id = ledger.bind(lease, session_intent, session_intent.key).key
    write = ModelToolCall(
        "write",
        "write_file",
        {"session_id": session_id, "path": "out", "content": "hello"},
    )
    with pytest.raises(PermissionError, match="read-only"):
        ledger.native_effect(
            lease,
            write,
            machine=machine,
            scopes=scopes,
            expires=None,
            registered=(machine,),
            claim=True,
        )


@pytest.mark.asyncio
async def test_team_graph_saver_and_member_activation_are_isolated(setup):
    from uuid import uuid4

    pytest.importorskip("langgraph")
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint

    from workgate.hosted_chat.checkpoints import SQLiteCheckpointSaver
    from workgate.hosted_chat.graph_checkpoint import OwnedGraphCheckpointer
    from workgate.hosted_chat.subagent_scope import SubagentGraphScope

    lease, parent, child, leaf = setup
    repo = AgentRepository(lease.database)
    parent = repo.update(
        "alice",
        parent["id"],
        {
            "subagents": {
                "enabled": True,
                "allowSelf": False,
                "graphs": [
                    {
                        "type": "team",
                        "name": "Team",
                        "description": "Review",
                        "agent_ids": [child["id"], leaf["id"]],
                        "entry_agent_id": child["id"],
                        "result_agent_id": leaf["id"],
                        "edges": [
                            {
                                "from": child["id"],
                                "to": leaf["id"],
                                "edgeType": "direct",
                            }
                        ],
                    }
                ],
            },
        },
    )
    assert parent is not None
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, "team")
    value = await invocation(root, parent, call)
    scope = SubagentScope.admit(root, call, value)
    graph_scope = SubagentGraphScope(scope)
    member = SubagentGraphScope(scope, leaf["id"], str(uuid4()))
    with pytest.raises(PermissionError, match="no admitted checkpoint"):
        OwnedLoopCheckpoint(lease, scope=member)
    saver = OwnedGraphCheckpointer(
        lease.database, lease.owner, actor_lease=lease, scope=graph_scope
    )
    config = cast(
        RunnableConfig,
        saver.storage.config(
            "chat", graph_scope.graph_namespace, event_actor=True
        ),
    )
    assert saver.get_tuple(config) is None
    with lease.database.connect() as db:
        state = json.loads(
            db.execute("SELECT state FROM runs WHERE id='run'").fetchone()[
                "state"
            ]
        )
        state.setdefault("ownedGraphCheckpoints", {})[
            graph_scope.graph_namespace
        ] = "team"
        db.execute(
            "UPDATE runs SET state=? WHERE id='run'", (json.dumps(state),)
        )
    graph_state = empty_checkpoint()
    graph_state["id"] = "graph1"
    saved = saver.put(config, graph_state, {"source": "input", "step": 0}, {})
    assert saver.get_tuple(saved) is not None
    owned = OwnedLoopCheckpoint(lease, scope=member)
    assert owned.restore(context(leaf)) is None
    await owned.save(
        AgentLoopState(
            context(leaf),
            [{"role": "assistant", "content": "member only"}],
            completed=True,
        ),
        [],
    )
    assert owned.restore(context(leaf)) is not None
    other = SQLiteCheckpointSaver(
        lease.database, lease.owner, actor_lease=lease
    )
    for private in (config, owned.base):
        with pytest.raises(ActorLeaseLost):
            other.get_tuple(private)
        with pytest.raises(ActorLeaseLost):
            list(other.list(private))
    with pytest.raises(ActorLeaseLost):
        saver.storage.get_tuple(owned.base)
    with pytest.raises(ActorLeaseLost):
        owned.saver.get_tuple(config)
    with pytest.raises(PermissionError, match="outside the admitted"):
        OwnedLoopCheckpoint(
            lease, scope=SubagentGraphScope(scope, parent["id"], str(uuid4()))
        )
    with pytest.raises(PermissionError, match="scheduler"):
        OwnedLoopCheckpoint(lease, scope=graph_scope)
    with pytest.raises(PermissionError, match="scheduler"):
        OwnedGraphCheckpointer(
            lease.database, lease.owner, actor_lease=lease, scope=member
        )
    with lease.database.read_snapshot() as db:
        assert [a["id"] for a in member.resolve(db, lease) if a] == [
            parent["id"],
            child["id"],
            leaf["id"],
        ]
    lease.release()
    replacement = ActorLease.claim(lease.database, lease.owner, lease.run_id)
    assert replacement is not None
    with pytest.raises(ActorLeaseLost):
        saver.get_tuple(saved)
    fresh = OwnedGraphCheckpointer(
        lease.database, lease.owner, actor_lease=replacement, scope=graph_scope
    )
    assert fresh.get_tuple(saved) is not None
    recovered = OwnedLoopCheckpoint(replacement, scope=member).restore(
        context(leaf)
    )
    assert recovered is not None and recovered.state.completed
    repo.update("alice", child["id"], {"instructions": "changed"})
    with pytest.raises(PermissionError, match="target revision changed"):
        fresh.get_tuple(saved)
