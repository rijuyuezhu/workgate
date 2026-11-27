"""Original-source differentials and owner-scoped isolated child execution."""

import asyncio
import itertools
import json
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLeaseLost
from workgate.hosted_chat.provider_common import ModelToolCall
from workgate.hosted_chat.subagents import (
    SubagentConfig,
    build_subagent_configs,
    build_subagent_tool_params,
    chat_subagent_config,
    filter_subagent_result,
    invoke_subagent,
    resolve_subagent_max_turns,
    subagent_tools,
)


def _oracle(operation, inputs):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for original-source differential oracle")
    fixture = (
        Path(__file__).parent.parent
        / "fixtures/hosted_chat/subagent_reference.cjs"
    )
    program = """
const fs = require('fs'); const p = require(process.argv[1]);
const {operation,inputs} = JSON.parse(fs.readFileSync(0,'utf8'));
const output = inputs.map(input => {
 if(operation === 'schema') return p.buildSubagentToolParams(input.configs, input.options);
 if(operation === 'budget') return p.resolveSubagentMaxTurns(input.endpoint, input.agent);
 return p.filterSubagentResult(input.map(message => ({...message,
  _getType: () => message.role === 'assistant' ? 'ai' : message.role})));
}); process.stdout.write(JSON.stringify(output));
"""
    return json.loads(
        subprocess.check_output(
            [node, "-e", program, str(fixture.resolve())],
            input=json.dumps(
                {"operation": operation, "inputs": inputs}
            ).encode(),
        )
    )


def test_exact_dynamic_schema_and_capability_gating():
    configs = [
        SubagentConfig("self", "Main", "self task", {}, 16, "alice"),
        SubagentConfig(
            "agent_child", "Reviewer", "code review", {}, 16, "alice"
        ),
    ]
    cases = [
        {
            "configs": [
                {
                    "type": config.type,
                    "name": config.name,
                    "description": config.description,
                }
                for config in configs
            ],
            "options": {
                "background": background,
                "threadContinuation": continuation,
            },
        }
        for background, continuation in itertools.product(
            [False, True], repeat=2
        )
    ]
    assert [
        build_subagent_tool_params(
            configs,
            background=case["options"]["background"],
            thread_continuation=case["options"]["threadContinuation"],
        )
        for case in cases
    ] == _oracle("schema", cases)
    definition = subagent_tools(configs)[0]["function"]
    assert definition["name"] == "subagent"
    assert "run_in_background" not in definition["parameters"]["properties"]
    assert "subagent_thread_id" not in definition["parameters"]["properties"]
    assert subagent_tools([]) == []


def test_original_recursion_budget_cascade():
    cases = [
        {"agent": agent, "endpoint": endpoint}
        for agent, endpoint in itertools.product(
            [
                {},
                {"recursion_limit": 1},
                {"recursion_limit": 2},
                {"recursion_limit": 3},
                {"recursion_limit": 25},
                {"recursion_limit": 150},
                {"recursion_limit": -1},
                {"recursion_limit": "200"},
                {"recursion_limit": True},
            ],
            [
                {},
                {"recursionLimit": 75},
                {"maxRecursionLimit": 20},
                {"recursionLimit": 90, "maxRecursionLimit": 30},
                {"maxRecursionLimit": -1},
            ],
        )
    ]
    reference = _oracle("budget", cases)
    for case, expected in zip(cases, reference, strict=True):
        actual = resolve_subagent_max_turns(case["agent"], case["endpoint"])
        # The upstream implementation inserts a 50-step implicit fallback.
        # LSM deliberately leaves the budget unset when no cap was selected.
        if actual is None:
            assert not (
                type(case["agent"].get("recursion_limit")) in (int, float)
                and case["agent"]["recursion_limit"] > 0
            )
            assert not any(
                type(case["endpoint"].get(key)) in (int, float)
                and case["endpoint"][key] > 0
                for key in ("recursionLimit", "maxRecursionLimit")
                if key in case["endpoint"]
            )
        else:
            assert actual == expected
    assert resolve_subagent_max_turns({}) is None
    assert resolve_subagent_max_turns({"recursion_limit": 30}) == 10


def test_original_result_projection_strips_reasoning_and_intermediate_tools():
    blocks = [
        "",
        "plain",
        {"type": "text", "text": "one"},
        {"type": "text", "annotations": []},
        {"type": "thinking", "thinking": "PRIVATE"},
        {"type": "tool_use", "name": "private"},
        {"type": "text_delta", "text": "hello", "index": 1},
        {"type": "text_delta", "text": " world", "index": 1},
        {"type": "text_delta", "text": "other", "index": 2},
        {"type": "text_delta", "text": " unindexed"},
        {"type": "text_delta", "text": "", "index": 3},
    ]
    cases = [
        [
            {"role": "assistant", "content": list(pair)},
            {"role": "tool", "content": "PRIVATE TOOL OUTPUT"},
        ]
        for pair in itertools.product(blocks, repeat=2)
    ]
    cases += [
        [],
        [{"role": "user", "content": "hello"}],
        [
            {"role": "assistant", "content": "earlier progress"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "unfinished"}],
            },
        ],
        [{"role": "assistant", "content": {"not": "text"}}],
        [{"role": "assistant", "content": ""}],
    ]
    assert [filter_subagent_result(case) for case in cases] == _oracle(
        "result", cases
    )
    assert (
        filter_subagent_result([{"role": "ai", "content": [None, 5, "ok"]}])
        == "ok"
    )


@pytest.fixture
def db(tmp_path):
    return SQLiteDatabase(tmp_path)


def _agent(db, owner="alice", **body):
    return AgentRepository(db).create(
        owner, {"provider": "openai", "model": None, "name": "Worker", **body}
    )


def test_optional_config_self_defaults_children_cycle_pruning_and_own_tools(db):
    child = _agent(
        db, tools=["file_read", "ask_user_question"], recursion_limit=30
    )
    parent = _agent(
        db,
        tools=["run_shell"],
        subagents={"enabled": True, "agent_ids": [child["id"]]},
    )
    child = AgentRepository(db).update(
        "alice",
        child["id"],
        {
            "subagents": {
                "enabled": True,
                "allowSelf": False,
                "agent_ids": [parent["id"]],
            },
        },
    )
    assert child is not None
    configs = build_subagent_configs("alice", parent, db)
    assert [config.type for config in configs] == ["self", child["id"]]
    assert configs[0].self_spawn and not configs[0].allow_nested
    assert "subagents" not in configs[0].agent
    assert configs[1].agent["tools"] == ["file_read"]
    assert configs[1].max_turns == 10
    assert configs[1].children == ()
    assert child["tools"] == ["file_read", "ask_user_question"]
    assert build_subagent_configs("alice", None, db) == []
    assert build_subagent_configs("alice", {"subagents": []}, db) == []
    assert (
        build_subagent_configs("alice", {"subagents": {"enabled": False}}, db)
        == []
    )
    parent["subagents"]["allowSelf"] = False
    parent["subagents"]["agent_ids"].append(parent["id"])
    assert len(build_subagent_configs("alice", parent, db)) == 1


def test_subagent_config_rejects_foreign_malformed_duplicate_and_graph(db):
    foreign = _agent(db, "bob")
    child = _agent(db)
    base = {"id": "parent", "subagents": {"enabled": True, "allowSelf": False}}
    for value in ["not-array", [7]]:
        base["subagents"]["agent_ids"] = value
        with pytest.raises(ValueError, match="agent_ids"):
            build_subagent_configs("alice", base, db)
    base["subagents"]["agent_ids"] = [foreign["id"]]
    with pytest.raises(PermissionError):
        build_subagent_configs("alice", base, db)
    base["subagents"]["agent_ids"] = [child["id"], child["id"]]
    with pytest.raises(ValueError, match="Duplicate"):
        build_subagent_configs("alice", base, db)
    base["subagents"]["agent_ids"] = []
    base["subagents"]["graphs"] = [{"id": "graph"}]
    with pytest.raises((ValueError, KeyError)):
        # Malformed team entries are rejected; valid source-defined teams run.
        build_subagent_configs("alice", base, db)
    with pytest.raises(ValueError, match="maximum depth"):
        build_subagent_configs("alice", base, db, depth=6)
    base["subagents"].pop("graphs")
    base["subagents"]["allowSelf"] = True
    with pytest.raises(ValueError, match="100 expanded"):
        build_subagent_configs("alice", base, db, _count=[100])
    base["subagents"]["allowSelf"] = False
    base["subagents"]["agent_ids"] = [child["id"]]
    with pytest.raises(ValueError, match="maximum depth"):
        build_subagent_configs("alice", base, db, depth=5)
    fallback = {"subagents": {"enabled": True}}
    assert build_subagent_configs("alice", fallback, db)[0].name == "self"


def test_saved_team_config_scope_revision_and_budget(db):
    repo = AgentRepository(db)
    root = _agent(db, recursion_limit=30)
    left = _agent(db, recursion_limit=12)
    right = _agent(db, recursion_limit=18)
    result = _agent(db, recursion_limit=24)
    parent = _agent(db)
    ids = [agent["id"] for agent in (root, left, right, result)]
    team = {
        "type": "review_team",
        "name": "Review team",
        "description": "Review and synthesize",
        "agent_ids": ids,
        "entry_agent_id": ids[0],
        "result_agent_id": ids[-1],
        "edges": [
            {"from": ids[0], "to": ids[1:3], "edgeType": "direct"},
            {"from": ids[1:3], "to": ids[-1], "edgeType": "direct"},
        ],
    }
    saved = repo.update(
        "alice",
        parent["id"],
        {"subagents": {"enabled": True, "allowSelf": False, "graphs": [team]}},
    )
    assert saved is not None
    (config,) = build_subagent_configs("alice", saved, db)
    assert config.type == "review_team"
    assert config.agent["graphAgentIds"] == ids
    assert config.agent["entryAgentId"] == ids[0]
    assert config.agent["resultAgentId"] == ids[-1]
    assert config.max_turns == 4
    assert config.children == ()
    assert not config.allow_nested
    assert root.get("edges") is None
    assert config.agent["edges"] == team["edges"]
    foreign = _agent(db, "bob")
    with pytest.raises(PermissionError):
        repo.update(
            "alice",
            parent["id"],
            {
                "subagents": {
                    "enabled": True,
                    "agent_ids": [foreign["id"]],
                }
            },
        )
    assert repo.get("alice", parent["id"]) == saved
    with pytest.raises(PermissionError):
        build_subagent_configs("bob", saved, db)
    assert repo.delete("alice", left["id"])
    with pytest.raises(PermissionError):
        build_subagent_configs("alice", saved, db)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "cycle",
        "split_fan_in",
        "non_direct",
        "foreign_endpoint",
        "wrong_result",
        "unconnected",
        "duplicate_type",
    ],
)
def test_team_invalid_topology_fails_before_persisting(db, mutation):
    repo = AgentRepository(db)
    a, b, c, d = [_agent(db)["id"] for _ in range(4)]
    parent = _agent(db)
    team = {
        "type": "team",
        "name": "Team",
        "description": "Review",
        "agent_ids": [a, b, c, d],
        "entry_agent_id": a,
        "result_agent_id": d,
        "edges": [
            {"from": a, "to": [b, c], "edgeType": "direct"},
            {"from": [b, c], "to": d, "edgeType": "direct"},
        ],
    }
    settings = {"enabled": True, "graphs": [team]}
    if mutation == "duplicate":
        team["agent_ids"].append(b)
    elif mutation == "cycle":
        team["edges"].append({"from": d, "to": a, "edgeType": "direct"})
    elif mutation == "split_fan_in":
        team["edges"][1:] = [
            {"from": b, "to": d, "edgeType": "direct"},
            {"from": c, "to": d, "edgeType": "direct"},
        ]
    elif mutation == "non_direct":
        team["edges"][0]["edgeType"] = "handoff"
    elif mutation == "foreign_endpoint":
        team["edges"][0]["from"] = "missing"
    elif mutation == "wrong_result":
        team["result_agent_id"] = a
    elif mutation == "unconnected":
        team["edges"][0]["to"] = b
    else:
        team["type"] = "self"
    with pytest.raises(ValueError):
        repo.update("alice", parent["id"], {"subagents": settings})
    assert repo.get("alice", parent["id"]) == parent


def test_saved_recursion_budget_validation(db):
    repo = AgentRepository(db)
    parent = _agent(db)
    for bad in [True, 0, -1, 1.5, 2**53, "30"]:
        with pytest.raises(ValueError, match="recursion_limit"):
            repo.update("alice", parent["id"], {"recursion_limit": bad})
    assert repo.get("alice", parent["id"]) == parent


def _invocation_options(db, **overrides):
    return {
        "owner": "alice",
        "conversation_id": "conversation-1",
        "parent_run_id": "run-1",
        "database": db,
        "current_grant": lambda: ({"shell:read"}, None),
        **overrides,
    }


@pytest.mark.asyncio
async def test_invocation_isolates_input_and_inherits_budget_and_scope(db):
    leaf = _agent(db)
    child = _agent(
        db,
        instructions="Be precise",
        tools=["file_read", "ask_user_question"],
        recursion_limit=30,
        subagents={"enabled": True, "agent_ids": [leaf["id"]]},
    )
    parent = _agent(
        db,
        instructions="PARENT PRIVATE",
        subagents={
            "enabled": True,
            "allowSelf": False,
            "agent_ids": [child["id"]],
        },
    )
    configs = build_subagent_configs("alice", parent, db)
    configs[0].agent["additional_instructions"] = "Use source"
    call = ModelToolCall(
        "call-1",
        "subagent",
        {"subagent_type": child["id"], "description": "Review"},
    )
    expiry = int(time.time()) + 60

    async def execute(invocation):
        assert invocation.messages == [
            {"role": "system", "content": "Be precise\n\nUse source"},
            {"role": "user", "content": "Review"},
        ]
        assert invocation.selected_tools == ("file_read",)
        assert invocation.scopes == frozenset({"shell:read"})
        assert invocation.grant_expires == expiry
        assert invocation.max_turns == 10
        assert invocation.recursion_limit == 30
        assert invocation.remaining_depth == 4
        assert invocation.parent_tool_call_id == "call-1"
        assert invocation.parent_run_id == "run-1"
        assert invocation.conversation_id == "conversation-1"
        assert invocation.owner == "alice"
        assert invocation.parent_agent_id == parent["id"]
        assert invocation.subagent_type == child["id"]
        assert [c.type for c in invocation.child_configs] == [
            "self",
            leaf["id"],
        ]
        invocation.agent["tools"].append("file_write")
        invocation.child_configs[0].agent["instructions"] = "Mutated"
        return [
            {"role": "assistant", "content": "intermediate"},
            {"role": "tool", "content": "private output"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "Final summary"},
                ],
            },
        ]

    options = _invocation_options(
        db,
        parent_agent_id=parent["id"],
        current_grant=lambda: ({"shell:read"}, expiry),
    )
    assert (
        await invoke_subagent(call, configs, execute_child=execute, **options)
        == "Final summary"
    )
    assert configs[0].agent["tools"] == ["file_read"]
    assert configs[0].children[0].agent["instructions"] == "Be precise"


@pytest.mark.asyncio
async def test_invocation_ordinary_chat_and_capability_gating(db):
    configs = [chat_subagent_config("alice")]
    options = _invocation_options(db)
    call = ModelToolCall("call-1", "subagent", {"subagent_type": "self"})

    async def execute(invocation):
        assert invocation.ordinary_chat
        assert invocation.messages == [
            {"role": "user", "content": "No task description provided"}
        ]
        assert invocation.remaining_depth == 0
        assert invocation.child_configs == ()
        assert invocation.selected_tools is None
        assert invocation.recursion_limit is None
        return "result"

    assert (
        await invoke_subagent(call, configs, execute_child=execute, **options)
        == "result"
    )
    assert "Unknown subagent type" in await invoke_subagent(
        replace(call, arguments={}), configs, execute_child=execute, **options
    )
    for depth in (0, 6):
        assert "nesting depth" in await invoke_subagent(
            call, configs, execute_child=execute, max_depth=depth, **options
        )
    with pytest.raises(ValueError, match="Unknown subagent"):
        await invoke_subagent(
            replace(call, name="delegate_agent"),
            configs,
            execute_child=execute,
            **options,
        )
    with pytest.raises(ValueError, match="object"):
        await invoke_subagent(
            replace(call, arguments=cast(dict, [])),
            configs,
            execute_child=execute,
            **options,
        )
    with pytest.raises(ValueError, match="host task store"):
        await invoke_subagent(
            replace(
                call,
                arguments={"subagent_type": "self", "run_in_background": True},
            ),
            configs,
            execute_child=execute,
            **options,
        )
    result = await invoke_subagent(
        replace(
            call,
            arguments={
                "subagent_type": "self",
                "subagent_thread_id": " thread ",
            },
        ),
        configs,
        execute_child=execute,
        **options,
    )
    assert json.loads(result)["status"] == "rejected"
    with pytest.raises(PermissionError, match="owned"):
        await invoke_subagent(
            call,
            [replace(configs[0], owner="bob")],
            execute_child=execute,
            **options,
        )
    with pytest.raises(PermissionError, match="ordinary chat"):
        await invoke_subagent(
            call,
            configs,
            execute_child=execute,
            **{**options, "parent_agent_id": "not-ordinary"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["parent", "child", "delete_child", "grant", "expire"]
)
async def test_invocation_rechecks_authority_before_returning_result(
    db, mutation
):
    repo = AgentRepository(db)
    child = _agent(db)
    parent = _agent(
        db,
        subagents={
            "enabled": True,
            "allowSelf": False,
            "agent_ids": [child["id"]],
        },
    )
    configs = build_subagent_configs("alice", parent, db)
    call = ModelToolCall(
        "call-1",
        "subagent",
        {"subagent_type": child["id"], "description": "Review"},
    )
    scopes, expiry = {"shell:read"}, None

    async def execute(invocation):
        nonlocal scopes, expiry
        if mutation == "parent":
            repo.update(
                "alice", parent["id"], {"subagents": {"enabled": False}}
            )
        elif mutation == "child":
            repo.update("alice", child["id"], {"instructions": "Changed"})
        elif mutation == "delete_child":
            repo.delete("alice", child["id"])
        elif mutation == "grant":
            scopes = set()
        else:
            expiry = 0
        return "private result"

    options = _invocation_options(
        db, parent_agent_id=parent["id"], current_grant=lambda: (scopes, expiry)
    )
    with pytest.raises(PermissionError):
        await invoke_subagent(call, configs, execute_child=execute, **options)

    async def must_not_execute(invocation):
        pytest.fail("stale invocation was admitted")

    if mutation == "grant":
        # The native caller rejects an already-revoked grant. An invocation
        # cannot infer the caller's previous scope set from an empty new grant.
        return
    with pytest.raises(PermissionError):
        await invoke_subagent(
            call, configs, execute_child=must_not_execute, **options
        )


@pytest.mark.asyncio
async def test_invocation_does_not_swallow_control_flow_or_leak_provider_errors(
    db,
):
    configs = [chat_subagent_config("alice")]
    call = ModelToolCall("call-1", "subagent", {"subagent_type": "self"})
    options = _invocation_options(db)

    async def failed(invocation):
        raise RuntimeError("https://secret.example/token")

    assert (
        await invoke_subagent(call, configs, execute_child=failed, **options)
        == "Subagent error: execution failed"
    )
    with pytest.raises(RuntimeError, match="secret.example"):
        await invoke_subagent(
            call,
            configs,
            execute_child=failed,
            propagate_error=lambda _: True,
            **options,
        )

    for error in (
        asyncio.CancelledError(),
        ActorLeaseLost("lost lease"),
        PermissionError("revoked"),
    ):

        async def interrupt(invocation, error=error):
            raise error

        with pytest.raises(type(error)):
            await invoke_subagent(
                call, configs, execute_child=interrupt, **options
            )


@pytest.mark.asyncio
async def test_invocation_machine_policy_and_depth_limit(db):
    child = _agent(
        db,
        tools=["file_read"],
        machine_default_permission="read",
        subagents={"enabled": True},
    )
    parent = _agent(
        db,
        subagents={
            "enabled": True,
            "allowSelf": False,
            "agent_ids": [child["id"]],
        },
    )
    configs = build_subagent_configs("alice", parent, db)
    call = ModelToolCall("call-1", "subagent", {"subagent_type": child["id"]})

    async def execute(invocation):
        assert invocation.selected_tools is None
        assert invocation.remaining_depth == 0
        assert invocation.child_configs == ()
        return "result"

    assert (
        await invoke_subagent(
            call,
            configs,
            execute_child=execute,
            max_depth=1,
            **_invocation_options(db, parent_agent_id=parent["id"]),
        )
        == "result"
    )


@pytest.mark.asyncio
async def test_invocation_team_rechecks_all_members(db):
    repo = AgentRepository(db)
    a, b = [_agent(db)["id"] for _ in range(2)]
    parent = _agent(
        db,
        subagents={
            "enabled": True,
            "allowSelf": False,
            "graphs": [
                {
                    "type": "team",
                    "name": "Team",
                    "description": "Review",
                    "agent_ids": [a, b],
                    "entry_agent_id": a,
                    "result_agent_id": b,
                    "edges": [{"from": a, "to": b, "edgeType": "direct"}],
                }
            ],
        },
    )
    configs = build_subagent_configs("alice", parent, db)
    call = ModelToolCall("call-1", "subagent", {"subagent_type": "team"})
    options = _invocation_options(db, parent_agent_id=parent["id"])

    async def execute(invocation):
        assert invocation.agent["graphAgentIds"] == [a, b]
        assert invocation.remaining_depth == 0
        assert repo.delete("alice", b)
        return "private result"

    with pytest.raises(PermissionError, match="owned"):
        await invoke_subagent(call, configs, execute_child=execute, **options)


@pytest.mark.asyncio
async def test_invocation_saved_self_is_bounded_and_rejects_wrong_parent(db):
    parent = _agent(db, subagents={"enabled": True})
    configs = build_subagent_configs("alice", parent, db)
    call = ModelToolCall("call-1", "subagent", {"subagent_type": "self"})

    async def execute(invocation):
        assert invocation.agent["id"] == parent["id"]
        assert not invocation.ordinary_chat
        assert invocation.remaining_depth == 0
        assert invocation.child_configs == ()
        return "result"

    assert (
        await invoke_subagent(
            call,
            configs,
            execute_child=execute,
            **_invocation_options(db, parent_agent_id=parent["id"]),
        )
        == "result"
    )
    other = _agent(db)
    with pytest.raises(PermissionError, match="parent revision"):
        await invoke_subagent(
            call,
            configs,
            execute_child=execute,
            **_invocation_options(db, parent_agent_id=other["id"]),
        )
