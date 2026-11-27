"""Real SDK graph execution with an owned activation boundary."""

import asyncio

import pytest

from workgate.hosted_chat.agent_runtime import RuntimeSafetyError
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.graph_runtime import (
    GraphMemberResult,
    run_multi_agent_graph,
)
from workgate.hosted_chat.handoff import (
    HandoffLimitError,
    build_handoff_command,
)


@pytest.fixture
def owned(tmp_path):
    pytest.importorskip("langgraph")
    db = SQLiteDatabase(tmp_path)
    repo = AgentRepository(db)
    agents = {
        name: repo.create(
            "alice",
            {
                "provider": "openai",
                "model": name,
                "instructions": name + " instructions",
            },
        )
        for name in ("root", "left", "right", "join")
    }
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id,owner) VALUES('chat','alice')"
        )
        conn.execute(
            "INSERT INTO runs(id,conversation_id,agent_id,status) VALUES('run','chat',?,'running')",
            (agents["root"]["id"],),
        )
    lease = ActorLease.claim(db, "alice", "run")
    assert lease is not None
    return repo, agents, lease


def configure(owned, edges, **fields):
    repo, agents, _ = owned

    def resolve(value):
        return (
            [resolve(v) for v in value]
            if isinstance(value, list)
            else agents[value]["id"]
        )

    return repo.update(
        "alice",
        agents["root"]["id"],
        {
            "edges": [
                {
                    **edge,
                    "from": resolve(edge["from"]),
                    "to": resolve(edge["to"]),
                }
                for edge in edges
            ],
            **fields,
        },
    )


async def run(owned, execute, *, history=None, lease=None, **kwargs):
    _, agents, original = owned
    return await run_multi_agent_graph(
        lease or original,
        agents["root"]["id"],
        history
        if history is not None
        else [
            {"role": "system", "content": "root override"},
            {"role": "user", "content": "start"},
        ],
        execute_member=execute,
        check_grant=lambda: None,
        recursion_limit=30,
        **kwargs,
    )


def reply(activation):
    return GraphMemberResult(
        [
            *activation.history,
            {
                "role": "assistant",
                "content": activation.agent["model"] + " answered",
            },
        ]
    )


def transfer(activation, root, targets):
    calls = [
        {
            "id": f"call-{target}",
            "type": "function",
            "function": {"name": "transfer_to_" + target, "arguments": "{}"},
        }
        for target in targets
    ]
    messages = [
        *activation.history,
        {"role": "assistant", "content": "", "tool_calls": calls},
    ]
    commands = []
    for target, call in zip(targets, calls, strict=True):
        edge = next(
            edge
            for edge in root["edges"]
            if edge["from"] == activation.agent["id"] and edge["to"] == target
        )
        command = build_handoff_command(
            edge,
            activation.agent["id"],
            activation.agent["name"],
            call["function"]["name"],
            {},
            call["id"],
            messages,
        )
        assert command is not None
        commands.append(command)
    return GraphMemberResult(messages, commands)


@pytest.mark.asyncio
async def test_direct_fanout_join_barrier_and_resume(owned):
    repo, agents, lease = owned
    configure(
        owned,
        [
            {"from": "root", "to": ["left", "right"]},
            {"from": ["left", "right"], "to": "join", "edgeType": "direct"},
        ],
    )
    seen = []
    arrived = set()
    both = asyncio.Event()

    async def execute(activation):
        name = activation.agent["model"]
        seen.append((name, activation.activation_id))
        system = [
            m["content"] for m in activation.history if m["role"] == "system"
        ]
        assert system == [
            "root override" if name == "root" else name + " instructions"
        ]
        if name in {"left", "right"}:
            arrived.add(name)
            if len(arrived) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
        if name == "join":
            assert {
                m["content"]
                for m in activation.history
                if m["role"] == "assistant"
            } == {"root answered", "left answered", "right answered"}
        return reply(activation)

    result = await run(owned, execute)
    assert [name for name, _ in seen[:1]] == ["root"]
    assert {name for name, _ in seen[1:3]} == {"left", "right"}
    assert seen[-1][0] == "join"
    assert len({ident for _, ident in seen}) == 4
    lease.release()
    replacement = ActorLease.claim(repo.database, "alice", "run")
    assert replacement is not None
    again = await run(owned, execute, history=[], lease=replacement)
    assert result == again
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_prompted_join_excludes_results_and_returns_designated_result(
    owned,
):
    _, agents, _ = owned
    configure(
        owned,
        [
            {"from": "root", "to": ["left", "right"]},
            {
                "from": ["left", "right"],
                "to": "join",
                "edgeType": "direct",
                "prompt": "summarize {results}",
            },
        ],
        resultAgentId=agents["join"]["id"],
    )

    async def execute(activation):
        if activation.agent["model"] == "join":
            assert not any(m["role"] == "assistant" for m in activation.history)
            prompt = activation.history[-1]["content"]
            assert "left answered" in prompt and "right answered" in prompt
        return reply(activation)

    result = await run(owned, execute, result_only=True)
    assert [m["content"] for m in result] == ["join answered"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_parallel_transfers_and_shared_budget(owned, limit):
    _, agents, _ = owned
    root = configure(
        owned,
        [
            {"from": "root", "to": "left", "edgeType": "handoff"},
            {"from": "root", "to": "right", "edgeType": "handoff"},
        ],
        maxHandoffs=limit,
    )
    assert root is not None
    seen = []

    async def execute(activation):
        name = activation.agent["model"]
        seen.append(name)
        if name == "root":
            targets = [agents[v]["id"] for v in ("left", "right")]
            assert {
                tool["function"]["name"] for tool in activation.handoff_tools
            } == {"transfer_to_" + v for v in targets}
            return transfer(activation, root, targets)
        assert not any(
            m.get("role") == "tool" or m.get("tool_calls")
            for m in activation.history
        )
        assert len(activation.handoff_state["transitions"]) == 2
        return reply(activation)

    if limit == 1:
        with pytest.raises(HandoffLimitError):
            await run(owned, execute)
        assert seen == ["root"]
    else:
        result = await run(owned, execute)
        assert seen[0] == "root" and set(seen[1:]) == {"left", "right"}
        assert sum(m["role"] == "tool" for m in result) == 2
        assert {m["content"] for m in result if m["role"] == "assistant"} >= {
            "left answered",
            "right answered",
        }


@pytest.mark.asyncio
async def test_mixed_handoff_uses_exclusive_command_path(owned):
    _, agents, _ = owned
    root = configure(
        owned,
        [
            {"from": "root", "to": "left", "edgeType": "handoff"},
            {"from": "root", "to": "right", "edgeType": "direct"},
        ],
    )
    assert root is not None
    seen = []

    async def execute(activation):
        name = activation.agent["model"]
        seen.append(name)
        if name == "root":
            return transfer(activation, root, [agents["left"]["id"]])
        return reply(activation)

    await run(owned, execute)
    assert seen == ["root", "left"]


@pytest.mark.asyncio
async def test_deleted_member_and_changed_graph_fail_closed(owned):
    repo, agents, lease = owned
    configure(owned, [{"from": "root", "to": "left", "edgeType": "direct"}])

    async def execute(activation):
        repo.delete("alice", agents["left"]["id"])
        return reply(activation)

    with pytest.raises(PermissionError, match="revoked"):
        await run(owned, execute)
    with pytest.raises(PermissionError, match="not owned"):
        await run(owned, execute)
    # Changing topology cannot reuse the durable graph's old checkpoint.
    configure(owned, [{"from": "root", "to": "right", "edgeType": "direct"}])
    with pytest.raises(RuntimeSafetyError, match="checkpoint or definition"):
        await run(owned, execute)


@pytest.mark.asyncio
async def test_checkpoint_expiry_cannot_restart_effects(owned):
    repo, _, lease = owned
    configure(owned, [{"from": "root", "to": "left", "edgeType": "direct"}])
    seen = []

    async def execute(activation):
        seen.append(activation.agent["model"])
        return reply(activation)

    await run(owned, execute)
    with repo.database.connect() as conn:
        conn.execute("UPDATE agent_checkpoints SET expires_at=0")
    with pytest.raises(RuntimeSafetyError, match="checkpoint or definition"):
        await run(owned, execute)
    assert seen == ["root", "left"]


@pytest.mark.asyncio
async def test_direct_fallback_when_no_transfer(owned):
    configure(
        owned,
        [
            {"from": "root", "to": "left", "edgeType": "handoff"},
            {"from": "root", "to": "right", "edgeType": "direct"},
        ],
    )
    seen = []

    async def execute(activation):
        seen.append(activation.agent["model"])
        return reply(activation)

    await run(owned, execute)
    assert seen == ["root", "right"]


@pytest.mark.asyncio
async def test_same_member_reentry_gets_distinct_activation(owned):
    _, agents, _ = owned
    root = configure(
        owned,
        [
            {"from": "root", "to": "left", "edgeType": "handoff"},
            {"from": "left", "to": "root", "edgeType": "handoff"},
        ],
        maxHandoffs=2,
    )
    assert root is not None
    seen = []

    async def execute(activation):
        name = activation.agent["model"]
        seen.append((name, activation.activation_id))
        if len(seen) < 3:
            return transfer(
                activation,
                root,
                [agents["left" if name == "root" else "root"]["id"]],
            )
        return reply(activation)

    await run(owned, execute)
    assert [name for name, _ in seen] == ["root", "left", "root"]
    assert len({ident for _, ident in seen}) == 3


@pytest.mark.asyncio
async def test_completed_sibling_not_replayed_on_failed_parallel_superstep(
    owned,
):
    repo, _, lease = owned
    configure(
        owned,
        [
            {"from": "root", "to": ["left", "right"]},
            {"from": ["left", "right"], "to": "join", "edgeType": "direct"},
        ],
    )
    seen = []
    done = asyncio.Event()

    async def fail(activation):
        name = activation.agent["model"]
        seen.append((name, activation.activation_id))
        if name == "left":
            done.set()
        if name == "right":
            await done.wait()
            await asyncio.sleep(0)
            raise RuntimeError("simulated failed sibling")
        return reply(activation)

    with pytest.raises(RuntimeError, match="failed sibling"):
        await run(owned, fail)
    lease.release()
    replacement = ActorLease.claim(repo.database, "alice", "run")
    assert replacement is not None

    async def resume(activation):
        seen.append((activation.agent["model"], activation.activation_id))
        return reply(activation)

    result = await run(owned, resume, history=[], lease=replacement)
    assert [name for name, _ in seen].count("root") == 1
    assert [name for name, _ in seen].count("left") == 1
    right = [ident for name, ident in seen if name == "right"]
    assert len(right) == 2 and right[0] == right[1]
    assert result[-1]["content"] == "join answered"


@pytest.mark.asyncio
async def test_foreign_root_wrong_generation_and_invalid_transfer(owned):
    repo, agents, lease = owned
    root = configure(
        owned, [{"from": "root", "to": "left", "edgeType": "handoff"}]
    )
    assert root is not None

    async def should_not_run(activation):
        pytest.fail("unauthorized graph member activated")

    with pytest.raises(PermissionError, match="admitted generation"):
        await run_multi_agent_graph(
            lease,
            agents["right"]["id"],
            [],
            execute_member=should_not_run,
            check_grant=lambda: None,
            recursion_limit=30,
        )
    foreign = repo.create("bob", {"provider": "openai", "model": "foreign"})
    with pytest.raises(PermissionError, match="not owned"):
        await run_multi_agent_graph(
            lease,
            foreign["id"],
            [],
            execute_member=should_not_run,
            check_grant=lambda: None,
            recursion_limit=30,
            namespace="child",
        )

    async def invalid(activation):
        result = transfer(activation, root, [agents["left"]["id"]])
        result.commands[0]["goto"] = agents["right"]["id"]
        return result

    with pytest.raises(PermissionError, match="not declared"):
        await run(owned, invalid)


@pytest.mark.asyncio
async def test_grant_revoked_at_superstep_boundary(owned):
    _, agents, lease = owned
    configure(owned, [{"from": "root", "to": "left", "edgeType": "direct"}])
    revoked = False

    def grant():
        if revoked:
            raise PermissionError("grant revoked")

    async def execute(activation):
        nonlocal revoked
        assert activation.agent["model"] == "root"
        revoked = True
        return reply(activation)

    with pytest.raises(PermissionError, match="grant revoked"):
        await run_multi_agent_graph(
            lease,
            agents["root"]["id"],
            [],
            execute_member=execute,
            check_grant=grant,
            recursion_limit=30,
        )


@pytest.mark.asyncio
async def test_member_checkpoint_and_receipts_are_isolated_and_recover_exact_activation(
    owned,
):
    from dataclasses import replace

    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.generation_actor_lease import ActorLeaseLost
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.loop_receipts import execute_checkpointed_batch
    from workgate.hosted_chat.provider_common import (
        ModelToolCall,
        ProviderConfig,
    )

    repo, agents, lease = owned
    configure(owned, [{"from": "root", "to": ["left", "right"]}])
    scopes = {}
    executed = []

    async def execute(activation):
        scope = activation.scope
        name = activation.agent["model"]
        scopes[name] = scope
        checkpoint = OwnedLoopCheckpoint(lease, scope=scope)
        context = AgentContext(
            ProviderConfig(
                "openai", name, "https://provider.invalid", "secret"
            ),
            [],
            agent=activation.agent,
        )
        assert checkpoint.restore(context) is None
        call = ModelToolCall("same-provider-call", "read", {"path": name})
        state = AgentLoopState(
            context,
            activation.history,
            step=2,
            model_turn=1,
            pending_calls=[call],
            tool_step_started=True,
        )
        await checkpoint.save(state, [call])

        async def effect(value):
            executed.append((name, value.call_id))
            return {"text": name}

        assert await execute_checkpointed_batch(
            checkpoint, [call], effect, check_grant=lambda: None
        ) == [{"text": name}]
        claims = ApprovalClaimStore(repo.database)
        with pytest.raises(PermissionError, match="graph-aware"):
            claims.pause(checkpoint, [call])
        assert OwnedLoopCheckpoint(lease).restore(context) is None
        return reply(activation)

    await run(owned, execute)
    assert len(executed) == 3
    assert (
        len(
            {
                scope.native_call_id("same-provider-call")
                for scope in scopes.values()
            }
        )
        == 3
    )
    old = OwnedLoopCheckpoint(lease, scope=scopes["left"])
    lease.release()
    fresh = ActorLease.claim(repo.database, "alice", "run")
    assert fresh is not None
    with pytest.raises(ActorLeaseLost):
        old.assert_current()
    with pytest.raises(PermissionError, match="identity changed"):
        OwnedLoopCheckpoint(
            fresh,
            scope=replace(scopes["left"], member_id=agents["right"]["id"]),
        )
    with pytest.raises(PermissionError, match="namespace"):
        OwnedLoopCheckpoint(
            fresh,
            scope=replace(
                scopes["left"], graph_namespace="other:graph:foreground"
            ),
        )
    with pytest.raises(PermissionError, match="no admitted"):
        OwnedLoopCheckpoint(
            fresh,
            scope=replace(scopes["left"], graph_namespace="run:graph:absent"),
        )
    with pytest.raises(ValueError, match="UUID|badly formed"):
        OwnedLoopCheckpoint(
            fresh, scope=replace(scopes["left"], activation_id="invalid")
        )
    for name, scope in scopes.items():
        checkpoint = OwnedLoopCheckpoint(fresh, scope=scope)
        context = AgentContext(
            ProviderConfig(
                "openai", name, "https://provider.invalid", "new-secret"
            ),
            [],
            agent=agents[name],
        )
        recovery = checkpoint.restore(context)
        assert recovery is not None
        with pytest.raises(RuntimeSafetyError, match="member context"):
            checkpoint.restore(
                AgentContext(context.profile, [], agent={"id": "wrong"})
            )

        async def forbidden(value):
            pytest.fail("A settled sibling receipt must not execute twice")

        assert await execute_checkpointed_batch(
            checkpoint,
            recovery.state.pending_calls,
            forbidden,
            check_grant=lambda: None,
        ) == [{"text": name}]
    # Expiration cannot turn one old member into a new model/native activation.
    with repo.database.connect() as conn:
        conn.execute(
            "UPDATE agent_checkpoints SET expires_at=0 WHERE checkpoint_ns=?",
            (scopes["left"].checkpoint_namespace,),
        )
    context = AgentContext(
        ProviderConfig(
            "openai", "left", "https://provider.invalid", "new-secret"
        ),
        [],
        agent=agents["left"],
    )
    with pytest.raises(RuntimeSafetyError, match="no longer available"):
        OwnedLoopCheckpoint(fresh, scope=scopes["left"]).restore(context)
    # A different member remains recoverable; a changed root fences all.
    right = OwnedLoopCheckpoint(fresh, scope=scopes["right"])
    assert (
        right.restore(AgentContext(context.profile, [], agent=agents["right"]))
        is not None
    )
    repo.update("alice", agents["root"]["id"], {"name": "new definition"})
    with pytest.raises(PermissionError, match="definition"):
        right.assert_current()
    with pytest.raises(PermissionError, match="definition"):
        right.saver.put_writes(right.active, [("messages", {})], "stale")
