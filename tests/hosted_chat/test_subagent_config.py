"""Original-source differentials and owner-scoped isolated child execution."""

import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.subagents import (
    SubagentConfig,
    build_subagent_configs,
    build_subagent_tool_params,
    filter_subagent_result,
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
