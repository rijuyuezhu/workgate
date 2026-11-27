"""Original SDK construction analysis; execution support is tested separately."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from workgate.hosted_chat.graph_topology import GraphTopology, admit_agent_graph

FIXTURES = Path(__file__).parent.parent / "fixtures/hosted_chat"
VECTORS = json.loads(
    (FIXTURES / "graph_topology_vectors.json").read_text(encoding="utf-8")
)


def test_original_sdk_topology_cases_match_python():
    for index, (case, expected) in enumerate(
        zip(VECTORS["cases"], VECTORS["expected"], strict=True)
    ):
        try:
            result = GraphTopology(
                case["agentIds"],
                case["edges"],
                entry_agent_id=case.get("entryAgentId"),
                result_agent_id=case.get("resultAgentId"),
                member_recursion_limit=case.get("memberRecursionLimit"),
                max_handoffs=case.get("maxHandoffs"),
            ).summary()
        except ValueError as error:
            result = {"error": str(error)}
        assert result == expected, (index, case, result, expected)


def test_original_sdk_topology_fixture_provenance_and_live_execution():
    manifest = json.loads(
        (FIXTURES / "graph_topology_sources.json").read_text(encoding="utf-8")
    )
    assert VECTORS["sources"] == manifest
    assert manifest["commit"] == "c4ffb9b1b78421bf63eba3e56993017f922387f3"
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the live original SDK oracle")
    program = """const fs=require('fs'),run=require(process.argv[1]);
const cases=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(cases.map(c=>{
try{return run(c);}catch(error){return {error:error.message};}})));"""
    expected = json.loads(
        subprocess.check_output(
            [
                node,
                "-e",
                program,
                str(FIXTURES / "graph_topology_reference.cjs"),
            ],
            input=json.dumps(VECTORS["cases"]).encode("utf-8"),
        )
    )
    assert expected == VECTORS["expected"]


def test_graph_admission_preserves_all_direct_and_handoff_edges():
    repository = SimpleNamespace(
        get=lambda owner, ident: (
            {"id": ident} if owner == "alice" and ident in "abcd" else None
        )
    )
    assert admit_agent_graph("alice", None, repository) is None
    assert (
        admit_agent_graph("alice", {"id": "a", "edges": []}, repository) is None
    )
    topology = admit_agent_graph(
        "alice", {"id": "a", "edges": [{"from": "a", "to": "b"}]}, repository
    )
    assert topology is not None
    assert topology.handoff_edges == [{"from": "a", "to": "b"}]
    cycle = admit_agent_graph(
        "alice",
        {
            "id": "a",
            "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
        },
        repository,
    )
    assert cycle is not None
    assert cycle.starting_nodes == ["a"]
    with pytest.raises(PermissionError, match="not owned"):
        admit_agent_graph(
            "bob", {"id": "a", "edges": [{"from": "a", "to": "b"}]}, repository
        )
    for edge in (
        {"from": "a", "to": "b", "edgeType": "direct"},
        {"from": "a", "to": ["b", "c"]},
    ):
        topology = admit_agent_graph(
            "alice", {"id": "a", "edges": [edge]}, repository
        )
        assert topology is not None
        assert topology.direct_edges == [edge]
    topology = admit_agent_graph(
        "alice",
        {
            "id": "a",
            "edges": [{"from": "a", "to": "b"}, {"from": "c", "to": "d"}],
        },
        repository,
    )
    assert topology is not None
    assert topology.starting_nodes == ["a", "c"]


@pytest.mark.parametrize(
    "patch",
    [
        {"edges": {}},
        {"edges": [None]},
        {"edges": [{"from": "a", "to": []}]},
        {"edges": [{"from": {"agent": "a"}, "to": "b"}]},
        {"edges": [{"to": "b"}]},
        {"graphAgentIds": "b"},
        {"graphAgentIds": [{}]},
        {"id": ""},
    ],
)
def test_owned_admission_rejects_malformed_graph_before_repository_lookup(
    patch,
):
    repository = SimpleNamespace(
        get=lambda *_: pytest.fail("Invalid input must not reach repository")
    )
    with pytest.raises(ValueError):
        admit_agent_graph(
            "alice",
            {"id": "a", "edges": [{"from": "a", "to": "b"}], **patch},
            repository,
        )


def test_owned_admission_uses_actual_agent_repository(tmp_path):
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.database import SQLiteDatabase

    repository = AgentRepository(SQLiteDatabase(tmp_path))
    parent, child = [
        repository.create("alice", {"provider": "openai", "model": name})
        for name in ("parent", "child")
    ]
    foreign = repository.create(
        "bob", {"provider": "openai", "model": "foreign"}
    )
    edge = {"from": parent["id"], "to": child["id"], "edgeType": "direct"}
    graph = {**parent, "edges": [edge]}
    result = admit_agent_graph("alice", graph, repository)
    assert result is not None and result.direct_edges == [edge]
    with pytest.raises(PermissionError, match="not owned"):
        admit_agent_graph(
            "alice", {**graph, "graphAgentIds": [foreign["id"]]}, repository
        )
    assert repository.delete("alice", child["id"])
    with pytest.raises(PermissionError, match="not owned"):
        admit_agent_graph("alice", graph, repository)
