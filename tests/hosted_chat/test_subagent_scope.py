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
from workgate.hosted_chat.detached_subagents import reserve_detached_child
from workgate.hosted_chat.durable_tasks import DurableTaskRepository
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig
from workgate.hosted_chat.subagent_scope import (
    DetachedExecutionScope,
    SubagentScope,
)
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
    checkpoint,
    source,
    target,
    *,
    call_id="same",
    description="child task",
    background=False,
    thread_id=None,
):
    call = ModelToolCall(
        call_id,
        "subagent",
        {"subagent_type": target, "description": description},
    )
    if background:
        call.arguments["run_in_background"] = True
    if thread_id is not None:
        call.arguments["subagent_thread_id"] = thread_id
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
        execute_background=execute,
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
    scope.assert_team_root(value.agent)
    with pytest.raises(PermissionError, match="definition"):
        scope.assert_team_root({**value.agent, "resultAgentId": child["id"]})
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


@pytest.mark.asyncio
async def test_detached_reservation_replay_and_no_foreground_authority(setup):
    lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"], background=True)
    value = await invocation(root, parent, call)
    with pytest.raises(PermissionError, match="identity"):
        SubagentScope.admit(root, call, value)
    reservation = reserve_detached_child(root, call, value)
    with lease.database.read_snapshot() as db:
        row = db.execute("SELECT * FROM subagent_tasks").fetchone()
        scope = SubagentScope.from_record(
            json.loads(row["execution_options"])["scope"]
        )
        assert row["child_run_id"] is None and row["child_registered"] == 1
        assert json.loads(row["initial_messages"]) == value.messages
        assert (
            db.execute(
                "SELECT active FROM active_subagent_threads WHERE owner=? AND thread_id=?",
                (lease.owner, reservation.child_conversation_id),
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
        )
    assert scope.detached
    with pytest.raises(PermissionError, match="execution lease"):
        OwnedLoopCheckpoint(lease, scope=scope)
    with pytest.raises(PermissionError, match="execution lease"):
        DurableTaskRepository(lease.database, scope=scope)
    with pytest.raises(PermissionError, match="execution lease"):
        scope.native_call_id("tool")
    lease.release()
    fresh = ActorLease.claim(lease.database, lease.owner, lease.run_id)
    assert fresh is not None
    root = OwnedLoopCheckpoint(fresh)
    assert root.restore(context(parent)) is not None
    assert reserve_detached_child(root, call, value) == reservation
    with pytest.raises(PermissionError, match="reduced"):
        reserve_detached_child(root, call, replace(value, scopes=frozenset()))
    with pytest.raises(PermissionError, match="identity"):
        reserve_detached_child(root, call, replace(value, owner="bob"))
    another = await edge(root, parent, child["id"], background=True)
    with pytest.raises(PermissionError, match="edge"):
        SubagentScope.admit(
            root,
            replace(
                call, arguments={**call.arguments, "description": "different"}
            ),
            replace(value, messages=[{"role": "user", "content": "different"}]),
            detached=True,
        )
    assert (
        reserve_detached_child(
            root, another, await invocation(root, parent, another)
        ).task_id
        != reservation.task_id
    )


@pytest.mark.asyncio
async def test_detached_thread_continuation_fences_busy_scope_and_history(
    setup,
):
    lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"], background=True)
    reservation = reserve_detached_child(
        root,
        call,
        await invocation(root, parent, call),
        disclosure_access={"machines": ["one"]},
    )
    call = await edge(
        root,
        parent,
        child["id"],
        background=True,
        thread_id=reservation.thread_id,
    )
    value = await invocation(root, parent, call)
    with pytest.raises(ValueError, match="already running"):
        reserve_detached_child(
            root, call, value, disclosure_access={"machines": ["one"]}
        )
    history = [{"role": "assistant", "content": "prior result"}]
    with lease.database.connect() as db:
        db.execute(
            "UPDATE subagent_tasks SET status='completed' WHERE id=?",
            (reservation.task_id,),
        )
        db.execute(
            "UPDATE subagent_threads SET messages=? WHERE id=?",
            (json.dumps(history), reservation.thread_id),
        )
    reduced_call = await edge(
        root,
        parent,
        child["id"],
        background=True,
        thread_id=reservation.thread_id,
        description="reduced",
    )
    with pytest.raises(PermissionError, match="authority changed"):
        reserve_detached_child(
            root,
            reduced_call,
            await invocation(root, parent, reduced_call, scopes=set()),
            disclosure_access={"machines": ["one"]},
        )
    call = await edge(
        root,
        parent,
        child["id"],
        background=True,
        thread_id=reservation.thread_id,
        description="continue",
    )
    value = await invocation(root, parent, call)
    continued = reserve_detached_child(
        root, call, value, disclosure_access={"machines": ["one"]}
    )
    assert (
        continued.thread_id == reservation.thread_id
        and continued.child_conversation_id != reservation.child_conversation_id
    )
    with lease.database.read_snapshot() as db:
        row = db.execute(
            "SELECT initial_messages FROM subagent_tasks WHERE id=?",
            (continued.task_id,),
        ).fetchone()
        assert json.loads(row[0]) == history + value.messages
    missing = await edge(
        root, parent, child["id"], background=True, thread_id="not-owned"
    )
    with pytest.raises(PermissionError, match="unavailable"):
        reserve_detached_child(
            root, missing, await invocation(root, parent, missing)
        )


@pytest.mark.asyncio
async def test_detached_reservation_limit_and_expiry(setup, monkeypatch):
    from workgate.hosted_chat import detached_subagents

    monkeypatch.setattr(detached_subagents, "_RUNNING_PER_SCOPE", 1)
    lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"], background=True)
    value = await invocation(root, parent, call)
    with pytest.raises(PermissionError, match="expired"):
        reserve_detached_child(root, call, replace(value, grant_expires=1))
    reservation = reserve_detached_child(root, call, value)
    assert reserve_detached_child(root, call, value) == reservation
    call = await edge(root, parent, child["id"], background=True)
    with pytest.raises(ValueError, match="Too many"):
        reserve_detached_child(root, call, await invocation(root, parent, call))
    with lease.database.read_snapshot() as db:
        assert (
            db.execute("SELECT count(*) FROM subagent_tasks").fetchone()[0] == 1
        )
        assert (
            db.execute(
                "SELECT count(*) FROM active_subagent_threads"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_detached_lease_recovery_and_atomic_completion(setup):
    from workgate.hosted_chat.detached_task_lease import DetachedTaskLease

    parent_lease, parent, child, _ = setup
    checkpoint = OwnedLoopCheckpoint(parent_lease)
    call = await edge(checkpoint, parent, child["id"], background=True)
    value = await invocation(checkpoint, parent, call)
    reservation = reserve_detached_child(checkpoint, call, value)
    db = parent_lease.database
    grant = {"scopes": set(value.scopes), "expires": value.grant_expires}
    args = (db, "alice", "chat", reservation.task_id)
    with pytest.raises(PermissionError, match="owner scope"):
        DetachedTaskLease.claim(db, "bob", "chat", reservation.task_id, **grant)
    with pytest.raises(PermissionError, match="reduced"):
        DetachedTaskLease.claim(*args, scopes=set(), expires=None)
    parent_lease.release()
    with db.connect() as conn:
        conn.execute("UPDATE runs SET status='complete' WHERE id='run'")
    old = DetachedTaskLease.claim(*args, **grant)
    assert old is not None
    assert DetachedTaskLease.claim(*args, **grant) is None
    old.renew(**grant)
    with db.connect() as conn:
        conn.execute(
            "UPDATE subagent_tasks SET lease_until=0 WHERE id=?",
            (reservation.task_id,),
        )
    fresh = DetachedTaskLease.claim(*args, **grant)
    assert fresh is not None
    with pytest.raises(ActorLeaseLost):
        old.assert_current(**grant)
    old.release()
    fresh.assert_current(**grant)
    with pytest.raises(PermissionError, match="reduced"):
        fresh.renew(scopes=set(), expires=None)
    with pytest.raises(ValueError, match="admitted input"):
        fresh.complete([{"role": "assistant", "content": "wrong"}], **grant)
    fresh.complete(
        value.messages + [{"role": "assistant", "content": "final"}], **grant
    )
    with db.read_snapshot() as conn:
        row = conn.execute(
            "SELECT status,result,lease_owner FROM subagent_tasks WHERE id=?",
            (reservation.task_id,),
        ).fetchone()
        assert tuple(row) == ("completed", "final", None)
        marker = conn.execute(
            "SELECT active FROM active_subagent_threads WHERE thread_id=?",
            (reservation.child_conversation_id,),
        ).fetchone()
        assert marker[0] == 0
        assert (
            json.loads(
                conn.execute(
                    "SELECT messages FROM subagent_threads WHERE id=?",
                    (reservation.thread_id,),
                ).fetchone()[0]
            )[-1]["content"]
            == "final"
        )
    assert DetachedTaskLease.claim(*args, **grant) is None


@pytest.mark.asyncio
async def test_detached_lease_cancel_and_live_authority(setup):
    from workgate.hosted_chat.detached_task_lease import (
        DetachedTaskLease,
        request_cancel,
    )

    parent_lease, parent, child, _ = setup
    checkpoint = OwnedLoopCheckpoint(parent_lease)
    call = await edge(checkpoint, parent, child["id"], background=True)
    value = await invocation(checkpoint, parent, call)
    reservation = reserve_detached_child(
        checkpoint, call, value, disclosure_access={"machines": ["one"]}
    )
    db = parent_lease.database
    args = (db, "alice", "chat", reservation.task_id)
    grant = {
        "scopes": set(value.scopes),
        "expires": value.grant_expires,
        "disclosure_access": {"machines": ["one"]},
    }
    with pytest.raises(PermissionError, match="authority changed"):
        DetachedTaskLease.claim(*args, scopes=set(value.scopes), expires=None)
    lease = DetachedTaskLease.claim(*args, **grant)
    assert lease is not None
    with pytest.raises(PermissionError, match="not requested"):
        lease.acknowledge_cancel()
    with pytest.raises(PermissionError, match="expired"):
        lease.assert_current(**{**grant, "expires": 1})
    request_cancel(*args)
    with pytest.raises(ActorLeaseLost, match="cancellation"):
        lease.complete(value.messages, **grant)
    assert DetachedTaskLease.claim(*args, **grant) is None
    lease.acknowledge_cancel()
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT status FROM subagent_tasks WHERE id=?",
                (reservation.task_id,),
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            conn.execute(
                "SELECT active FROM active_subagent_threads WHERE thread_id=?",
                (reservation.child_conversation_id,),
            ).fetchone()[0]
            == 0
        )
    call = await edge(checkpoint, parent, child["id"], background=True)
    reservation = reserve_detached_child(
        checkpoint, call, await invocation(checkpoint, parent, call)
    )
    request_cancel(db, "alice", "chat", reservation.task_id)
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT status FROM subagent_tasks WHERE id=?",
                (reservation.task_id,),
            ).fetchone()[0]
            == "cancelled"
        )


@pytest.mark.asyncio
async def test_detached_reconcile_only_idle_reservations(setup):
    from workgate.hosted_chat.detached_task_lease import (
        DetachedTaskLease,
        reconcile_reservations,
        request_cancel,
    )

    parent_lease, parent, child, _ = setup
    checkpoint = OwnedLoopCheckpoint(parent_lease)
    call = await edge(checkpoint, parent, child["id"], background=True)
    value = await invocation(checkpoint, parent, call)
    reservation = reserve_detached_child(checkpoint, call, value)
    db = parent_lease.database
    lease = DetachedTaskLease.claim(
        db,
        "alice",
        "chat",
        reservation.task_id,
        scopes=set(value.scopes),
        expires=value.grant_expires,
    )
    assert lease is not None
    request_cancel(db, "alice", "chat", reservation.task_id)
    assert reconcile_reservations(db, "alice", "chat") == 0
    lease.release()
    assert reconcile_reservations(db, "bob", "chat") == 0
    assert reconcile_reservations(db, "alice", "chat") == 1
    assert reconcile_reservations(db, "alice", "chat") == 0
    call = await edge(checkpoint, parent, child["id"], background=True)
    reservation = reserve_detached_child(
        checkpoint, call, await invocation(checkpoint, parent, call)
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE subagent_tasks SET grant_expires=1 WHERE id=?",
            (reservation.task_id,),
        )
    assert reconcile_reservations(db, "alice", "chat") == 1
    with db.read_snapshot() as conn:
        row = conn.execute(
            "SELECT status,error FROM subagent_tasks WHERE id=?",
            (reservation.task_id,),
        ).fetchone()
        assert tuple(row) == ("error", "Authentication grant expired")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["complete", "cancel", "failure", "expired"]
)
async def test_detached_generation_is_private_and_settles_atomically(
    setup, outcome
):
    from workgate.hosted_chat.detached_task_lease import (
        DetachedTaskLease,
        reconcile_reservations,
        request_cancel,
    )
    from workgate.hosted_chat.generations import GenerationStore
    from workgate.hosted_chat.steering import SteeringStore, SteerRejection
    from workgate.hosted_chat.storage import WorkspaceRepository

    parent_lease, parent, child, _ = setup
    checkpoint = OwnedLoopCheckpoint(parent_lease)
    call = await edge(checkpoint, parent, child["id"], background=True)
    value = await invocation(checkpoint, parent, call)
    reservation = reserve_detached_child(checkpoint, call, value)
    db = parent_lease.database
    store = GenerationStore(db)
    workspace = WorkspaceRepository(db)
    cid = reservation.child_conversation_id
    args = (db, "alice", "chat", reservation.task_id)
    grant = {"scopes": set(value.scopes), "expires": value.grant_expires}
    with pytest.raises(PermissionError, match="read-only"):
        workspace.create_conversation("bob", {"conversationId": cid})
    task = DetachedTaskLease.claim(*args, **grant)
    assert task is not None
    generation, actor = task.materialize(**grant)
    assert generation.conversation_id == cid
    assert generation.agent_id == child["id"]
    assert task.materialize(**grant) == (generation, actor)
    with pytest.raises(ActorLeaseLost, match="task lease"):
        actor.renew()
    with pytest.raises(ActorLeaseLost, match="task lease"):
        actor.release()
    assert ActorLease.claim(db, "alice", generation.run_id) is None
    with pytest.raises(PermissionError, match="read-only"):
        store.begin("alice", {"conversationId": cid, "text": "foreign turn"})
    with pytest.raises(PermissionError, match="read-only"):
        workspace.update_conversation("alice", cid, {"title": "changed"})
    with pytest.raises(PermissionError, match="read-only"):
        workspace.add_message("alice", cid, {"text": "injected"})
    with pytest.raises(PermissionError, match="read-only"):
        workspace.delete_conversation("alice", cid)
    with pytest.raises(PermissionError, match="running detached children"):
        workspace.delete_conversation("alice", "chat")
    with pytest.raises(SteerRejection, match="CHILD_CONVERSATION_READ_ONLY"):
        SteeringStore(db).enqueue(
            "alice",
            {"conversationId": cid, "text": "injected"},
            preempt_capable=False,
            validate_submission=lambda _: None,
        )
    with pytest.raises(PermissionError, match="settle its task"):
        store.finish(
            "alice", generation.run_id, status="complete", actor_lease=actor
        )
    with pytest.raises(PermissionError, match="model deltas"):
        store.append("alice", generation.run_id, {}, actor_lease=actor)
    with pytest.raises(PermissionError, match="admitted execution scope"):
        DurableTaskRepository(db).tool_context_chain(actor)
    with pytest.raises(PermissionError, match="admitted execution scope"):
        OwnedLoopCheckpoint(actor)
    private = OwnedLoopCheckpoint(
        actor, scope=DetachedExecutionScope.from_actor(actor)
    )
    assert private.restore(context(child)) is None
    state = AgentLoopState(
        context(child),
        value.messages + [{"role": "assistant", "content": "final text"}],
        completed=True,
    )
    await private.save(state, [])
    with db.connect() as conn:
        conn.execute(
            "UPDATE subagent_tasks SET lease_until=0 WHERE id=?",
            (task.task_id,),
        )
    replacement = DetachedTaskLease.claim(*args, **grant)
    assert replacement is not None
    assert not actor.is_current()
    same, new_actor = replacement.materialize(**grant)
    assert same == generation
    assert new_actor.token != actor.token
    task.release()
    new_actor.assert_current()
    recovered = OwnedLoopCheckpoint(
        new_actor, scope=DetachedExecutionScope.from_actor(new_actor)
    ).restore(context(child))
    assert recovered is not None and recovered.state.messages == state.messages
    if outcome == "complete":
        replacement.complete(state.messages, **grant)
        expected = ("completed", "complete", "final text")
    elif outcome == "cancel":
        request_cancel(*args)
        assert not new_actor.is_current()
        replacement.acknowledge_cancel()
        expected = ("cancelled", "aborted", None)
    elif outcome == "failure":
        replacement.fail()
        expected = ("error", "error", None)
    else:
        replacement.release()
        with db.connect() as conn:
            conn.execute(
                "UPDATE subagent_tasks SET grant_expires=1 WHERE id=?",
                (task.task_id,),
            )
        assert reconcile_reservations(db, "alice", "chat") == 1
        expected = ("error", "error", None)
    with db.read_snapshot() as conn:
        row = conn.execute(
            "SELECT t.status,r.status,t.result FROM subagent_tasks t JOIN runs r ON r.id=t.child_run_id WHERE t.id=?",
            (task.task_id,),
        ).fetchone()
        assert tuple(row) == expected
        assert (
            conn.execute(
                "SELECT active FROM active_subagent_threads WHERE thread_id=?",
                (cid,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM runs WHERE conversation_id=?", (cid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM generation_events WHERE run_id=?",
                (generation.run_id,),
            ).fetchone()[0]
            == 1
        )
    assert not new_actor.is_current()
    assert ActorLease.claim(db, "alice", generation.run_id) is None
    with pytest.raises(PermissionError, match="read-only"):
        workspace.add_message("alice", cid, {"text": "after completion"})
    assert workspace.delete_conversation("alice", cid)
    with pytest.raises(PermissionError, match="read-only"):
        workspace.create_conversation("alice", {"conversationId": cid})


@pytest.mark.asyncio
async def test_detached_native_scope_keeps_parent_authority_and_child_resources(
    setup,
):
    from workgate.hosted_chat.detached_task_lease import DetachedTaskLease
    from workgate.hosted_chat.storage import WorkspaceRepository
    from workgate.protocol.ids import new_executor_id

    parent_lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(parent_lease)
    db = parent_lease.database
    machine = new_executor_id()
    registered = (machine,)
    scopes = {"shell:read", "shell:write", "shell:execute"}
    call = await edge(root, parent, child["id"], background=True)
    value = await invocation(root, parent, call, scopes=scopes)
    access = {
        "scopes": sorted(scopes),
        "machines": {machine: "read"},
        "task_machine": machine,
    }
    reservation = reserve_detached_child(
        root, call, value, disclosure_access=access
    )
    grant = {
        "scopes": scopes,
        "expires": value.grant_expires,
        "disclosure_access": access,
    }
    task_lease = DetachedTaskLease.claim(
        db, "alice", "chat", reservation.task_id, **grant
    )
    assert task_lease is not None
    generation, actor = task_lease.materialize(**grant)
    scope = DetachedExecutionScope.from_actor(actor)
    ledger = DurableTaskRepository(db, scope=scope)
    child_conversation, chain = ledger.tool_context_chain(actor)
    assert child_conversation == generation.conversation_id
    assert [a["id"] for a in chain if a] == [parent["id"], child["id"]]
    assert ledger.tool_context_chain(actor, authorization=True)[0] == "chat"
    with pytest.raises(PermissionError, match="requires its task actor"):
        ledger.tool_context_chain(parent_lease)
    workspace = WorkspaceRepository(db)
    workspace.save_setting(
        "alice", "agent.machine_policy.conversation.chat", {machine: "none"}
    )
    task = ModelToolCall("call", "task", {"action": "create"})
    with pytest.raises(PermissionError):
        ledger.reserve(
            actor,
            task,
            machine=machine,
            scopes=scopes,
            expires=None,
            registered=registered,
        )
    workspace.save_setting(
        "alice", "agent.machine_policy.conversation.chat", {machine: "write"}
    )
    with pytest.raises(PermissionError, match="verified child lineage"):
        ledger.reserve(
            actor,
            task,
            machine=machine,
            scopes=scopes,
            expires=None,
            registered=registered,
            authorization_conversation_id=child_conversation,
        )
    with pytest.raises(PermissionError, match="reduced"):
        ledger.reserve(
            actor,
            task,
            machine=machine,
            scopes={"shell:read"},
            expires=None,
            registered=registered,
        )
    with pytest.raises(PermissionError, match="admission ceiling"):
        ledger.reserve(
            actor,
            task,
            machine=new_executor_id(),
            scopes=scopes,
            expires=None,
            registered=registered,
        )
    intent = ledger.reserve(
        actor,
        task,
        machine=machine,
        scopes=scopes,
        expires=None,
        registered=registered,
    )
    assert ledger.claim(
        actor, intent, task, scopes=scopes, expires=None, registered=registered
    )
    ledger.bind(actor, intent, intent.key)
    assert ledger.owned("alice", child_conversation, "task", intent.key)
    assert ledger.cap_machine_access({machine: "write"}) == {machine: "read"}
    session = ModelToolCall(
        "session",
        "session_start",
        {"executor_id": machine, "task_id": intent.key},
    )
    session_intent = ledger.reserve(
        actor,
        session,
        machine=machine,
        scopes=scopes,
        expires=None,
        registered=registered,
    )
    assert ledger.claim(
        actor,
        session_intent,
        session,
        scopes=scopes,
        expires=None,
        registered=registered,
    )
    ledger.bind(actor, session_intent, session_intent.key)
    command = ModelToolCall(
        "command",
        "bash",
        {"session_id": session_intent.key, "command": "true", "async_": True},
    )
    with pytest.raises(PermissionError, match="admission ceiling"):
        ledger.reserve(
            actor,
            command,
            machine=machine,
            scopes=scopes,
            expires=None,
            registered=registered,
        )
    assert not ledger.owned("alice", "chat", "task", intent.key)
    with db.read_snapshot() as conn:
        row = conn.execute(
            "SELECT conversation_id,authorization_conversation_id,member_namespace FROM agent_durable_tasks WHERE id=?",
            (intent.id,),
        ).fetchone()
        assert tuple(row) == (
            child_conversation,
            "chat",
            scope.checkpoint_namespace,
        )
    await edge(root, parent, child["id"])
    parent_lease.release()
    with db.connect() as conn:
        conn.execute("UPDATE runs SET status='complete' WHERE id='run'")
    assert ledger.tool_context_chain(actor)[1] == chain
    task_lease.complete(
        value.messages + [{"role": "assistant", "content": "final"}], **grant
    )
    assert not actor.is_current()
    root_ledger = DurableTaskRepository(db)
    assert root_ledger.cancellation_batch(registered) == []
    workspace.save_setting(
        "alice", "agent.machine_policy.conversation.chat", {machine: "none"}
    )
    with db.connect() as conn:
        conn.execute("UPDATE agent_durable_tasks SET next_attempt_at=NULL")
    assert {
        row["id"] for row in root_ledger.cancellation_batch(registered)
    } == {intent.id, session_intent.id}


@pytest.mark.asyncio
async def test_detached_controls_claim_and_replay(setup):
    from workgate.hosted_chat.detached_task_controls import check_detached_task
    from workgate.hosted_chat.detached_task_lease import DetachedTaskLease

    lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"], background=True)
    value = await invocation(root, parent, call)
    reservation = reserve_detached_child(root, call, value)
    grant = {"scopes": set(value.scopes), "expires": value.grant_expires}
    db = lease.database

    async def control(call_id, **arguments):
        call = ModelToolCall(call_id, "check_background_task", arguments)
        state = AgentLoopState(
            context(parent),
            [],
            step=1,
            pending_calls=[call],
            tool_step_started=True,
        )
        await root.save(state, [call])
        return call

    listing = await control("list")
    result = check_detached_task(root, listing, **grant)
    assert result["outstanding"] == 1
    assert result["tasks"][0]["background_task_id"] == reservation.task_id
    assert "result" not in result["tasks"][0]
    poll = await control("poll", background_task_id=reservation.task_id)
    assert check_detached_task(root, poll, **grant)["status"] == "running"
    child_lease = DetachedTaskLease.claim(
        db, lease.owner, "chat", reservation.task_id, **grant
    )
    assert child_lease is not None
    child_lease.complete(
        value.messages + [{"role": "assistant", "content": "private final"}],
        **grant,
    )
    assert check_detached_task(root, poll, **grant)["status"] == "running"
    claim = await control("claim", background_task_id=reservation.task_id)
    with pytest.raises(PermissionError, match="reduced"):
        check_detached_task(root, claim, scopes=set(), expires=None)
    first = check_detached_task(root, claim, **grant)
    assert first["result"] == "private final"
    assert first["result_claimed"] is True
    assert check_detached_task(root, claim, **grant) == first
    with pytest.raises(PermissionError, match="reduced"):
        check_detached_task(root, claim, scopes=set(), expires=None)
    second = await control("second", background_task_id=reservation.task_id)
    result = check_detached_task(root, second, **grant)
    assert result["status"] == "claimed" and "result" not in result
    with pytest.raises(PermissionError, match="started parent edge"):
        check_detached_task(root, claim, **grant)
    foreign = await control("unknown", background_task_id="unknown")
    assert check_detached_task(root, foreign, **grant)["status"] == "not_found"


@pytest.mark.asyncio
async def test_detached_controls_cancel_fences_live_worker(setup):
    from workgate.hosted_chat.detached_task_controls import check_detached_task
    from workgate.hosted_chat.detached_task_lease import DetachedTaskLease

    lease, parent, child, _ = setup
    root = OwnedLoopCheckpoint(lease)
    call = await edge(root, parent, child["id"], background=True)
    value = await invocation(root, parent, call)
    reservation = reserve_detached_child(root, call, value)
    grant = {"scopes": set(value.scopes), "expires": value.grant_expires}
    worker = DetachedTaskLease.claim(
        lease.database, lease.owner, "chat", reservation.task_id, **grant
    )
    assert worker is not None
    cancel = ModelToolCall(
        "cancel",
        "check_background_task",
        {"background_task_id": reservation.task_id, "action": "cancel"},
    )
    state = AgentLoopState(
        context(parent),
        [],
        step=1,
        pending_calls=[cancel],
        tool_step_started=True,
    )
    await root.save(state, [cancel])
    assert (
        check_detached_task(root, cancel, **grant)["status"]
        == "cancellation_requested"
    )
    with pytest.raises(ActorLeaseLost, match="cancellation"):
        worker.assert_current(**grant)
    worker.acknowledge_cancel()
    assert (
        check_detached_task(root, cancel, **grant)["status"]
        == "cancellation_requested"
    )
    another = replace(cancel, call_id="cancel-again")
    state.pending_calls = [another]
    await root.save(state, [another])
    assert (
        check_detached_task(root, another, **grant)["status"] == "not_running"
    )
