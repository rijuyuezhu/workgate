"""Foreground delegation recovery, isolation and live authority."""

import time
from dataclasses import replace

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


async def invocation(checkpoint, source, call, *, configs=None, max_depth=5):
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
        current_grant=lambda: ({"shell:read"}, int(time.time()) + 600),
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
