"""JSON-facing port of LibreChat agents/validation.ts (e8f3be08).

Source: upstream v6.0.3, webui/agent_validation.py
(b500411155f878f295a682697cf9a0211532dbab). See LICENSE.upstream.
This unit validates core authoring only. Tool selection never grants access.
"""

import contextlib
import copy
import math
from typing import Any

_NUMERIC = frozenset(
    [
        "temperature",
        "top_p",
        "topP",
        "top_k",
        "topK",
        "frequency_penalty",
        "frequencyPenalty",
        "presence_penalty",
        "presencePenalty",
        "max_tokens",
        "maxTokens",
        "max_output_tokens",
        "maxOutputTokens",
        "max_context_tokens",
        "maxContextTokens",
        "fileTokenLimit",
        "thinking_budget",
        "thinkingBudget",
    ]
)


def _require(value: Any, kind: type, path: str) -> Any:
    if type(value) is not kind:
        raise ValueError(f"{path} must be {kind.__name__}")
    return value


def _strings(value: Any, path: str) -> list[str]:
    return [_require(item, str, path) for item in _require(value, list, path)]


def clean_edge(value: Any) -> dict:
    """Validate saved foreground edge fields from the pinned authoring source."""
    edge = _require(value, dict, "edge")
    result = {}
    for key in ("from", "to"):
        if key not in edge:
            raise ValueError(f"edge.{key} is required")
        item = edge[key]
        result[key] = (
            item if type(item) is str else _strings(item, "edge." + key)
        )
        if type(item) is list and not item:
            raise ValueError("Graph edge endpoints cannot be empty arrays")
    for key in ("description", "prompt", "promptKey"):
        if key in edge:
            value = _require(edge[key], str, "edge." + key)
            if value:
                result[key] = value
    for key, choices in (
        ("edgeType", ("handoff", "direct")),
        ("handoffScope", ("turn", "conversation")),
    ):
        if key in edge:
            value = edge[key]
            if type(value) is not str or value not in choices:
                raise ValueError(f"Invalid {key}")
            result[key] = value
    if "excludeResults" in edge:
        result["excludeResults"] = _require(
            edge["excludeResults"], bool, "excludeResults"
        )
    return result


def _clean_team_edge(value: Any) -> dict:
    edge = _require(value, dict, "edge")
    if set(edge) - {
        "from",
        "to",
        "description",
        "edgeType",
        "prompt",
        "excludeResults",
    }:
        raise ValueError("Graph subagent edges contain unknown fields")
    if edge.get("edgeType") != "direct":
        raise ValueError("Graph subagent edgeType must be direct")
    return clean_edge(edge)


def validate_subagent_topology(graph: dict) -> None:
    """Original validateGraphSubagentTopology checks, in source order."""
    ids = graph["agent_ids"]
    members = set(ids)
    prefix = f'Graph subagent "{graph["type"]}"'

    def reject(message):
        raise ValueError(prefix + " " + message)

    if len(members) != len(ids):
        reject("contains duplicate member IDs.")
    reserved = {
        "__start__",
        "__end__",
        "messages",
        "agentMessages",
        "subagentResult",
    }
    for identity in ids:
        if identity in reserved or "|" in identity or ":" in identity:
            reject(f'member "{identity}" is reserved by the graph runtime.')
    if (
        graph["entry_agent_id"] not in members
        or graph["result_agent_id"] not in members
    ):
        reject("entry and result must reference configured members.")
    adjacency = {identity: set() for identity in ids}
    reverse = {identity: set() for identity in ids}
    incoming: dict[str, list[list[str]]] = {}
    directed = set()
    for edge in graph["edges"]:
        sources = (
            edge["from"] if isinstance(edge["from"], list) else [edge["from"]]
        )
        destinations = (
            edge["to"] if isinstance(edge["to"], list) else [edge["to"]]
        )
        if len(set(sources)) != len(sources) or len(set(destinations)) != len(
            destinations
        ):
            reject("edge endpoints must be unique.")
        if edge.get("excludeResults") is True and not edge.get("prompt"):
            reject("cannot exclude results without an edge prompt.")
        if edge.get("prompt") and len(destinations) != 1:
            reject("prompted edges must have one destination.")
        for identity in sources + destinations:
            if identity not in members:
                reject(f'references unknown member "{identity}".')
        for destination in destinations:
            incoming.setdefault(destination, []).append(sources)
            for source in sources:
                if source == destination:
                    reject("cannot contain self-edges.")
                if (source, destination) in directed:
                    reject("contains duplicate edges.")
                directed.add((source, destination))
                adjacency[source].add(destination)
                reverse[destination].add(source)
    for destination, groups in incoming.items():
        if len(reverse[destination]) > 1 and (
            len(groups) != 1 or len(groups[0]) != len(reverse[destination])
        ):
            reject(
                f'fan-in to "{destination}" must use one array-valued source edge.'
            )
    roots = [identity for identity in ids if not reverse[identity]]
    sinks = [identity for identity in ids if not adjacency[identity]]
    if roots != [graph["entry_agent_id"]]:
        reject("must use entry_agent_id as its only root.")
    if sinks != [graph["result_agent_id"]]:
        reject("must use result_agent_id as its only sink.")
    remaining = {identity: len(reverse[identity]) for identity in ids}
    ready = list(roots)
    visited = 0
    while ready:
        source = ready.pop()
        visited += 1
        for destination in adjacency[source]:
            remaining[destination] -= 1
            if remaining[destination] == 0:
                ready.append(destination)
    if visited != len(members):
        reject("must be acyclic and fully connected.")
    chain = all(
        len(adjacency[identity]) <= 1 and len(reverse[identity]) <= 1
        for identity in ids
    )
    if not chain and any(
        edge.get("prompt")
        and (edge["to"][0] if isinstance(edge["to"], list) else edge["to"])
        != graph["result_agent_id"]
        for edge in graph["edges"]
    ):
        reject("prompts in a branched graph must target result_agent_id.")


def clean_subagents(value: Any, *, max_subagents: int = 10) -> dict:
    obj = _require(value, dict, "subagents")
    result = {
        key: _require(obj[key], bool, "subagents." + key)
        for key in ("enabled", "allowSelf", "shareFiles")
        if key in obj
    }
    if "agent_ids" in obj:
        result["agent_ids"] = _strings(obj["agent_ids"], "subagents.agent_ids")
        if len(result["agent_ids"]) > max_subagents:
            raise ValueError(
                f"agent_ids must contain at most {max_subagents} item(s)"
            )
    if "graphs" in obj:
        graphs = _require(obj["graphs"], list, "subagents.graphs")
        if len(graphs) > max_subagents:
            raise ValueError(
                f"graphs must contain at most {max_subagents} item(s)"
            )
        result["graphs"] = []
        for item in graphs:
            item = _require(item, dict, "graph")
            graph = {}
            for key in ("type", "name", "description"):
                text = _require(item.get(key), str, "graph." + key).strip()
                if not text:
                    raise ValueError("Graph " + key + " cannot be empty")
                graph[key] = text
            graph["agent_ids"] = _strings(
                item.get("agent_ids"), "graph.agent_ids"
            )
            if not 1 <= len(graph["agent_ids"]) <= 32:
                raise ValueError("Graph subagents require 1–32 members")
            graph["edges"] = [
                _clean_team_edge(edge)
                for edge in _require(item.get("edges"), list, "graph.edges")
            ]
            for key in ("entry_agent_id", "result_agent_id"):
                graph[key] = _require(item.get(key), str, "graph." + key)
            validate_subagent_topology(graph)
            result["graphs"].append(graph)
    ids = result.get("agent_ids", [])
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate subagent type")
    types = set(ids)
    configured = set(types)
    if result.get("allowSelf") is not False:
        types.add("self")
    for graph in result.get("graphs", []):
        if graph["type"] in types:
            raise ValueError(
                "Graph subagent types must be unique across all spawn targets"
            )
        types.add(graph["type"])
        configured.update(graph["agent_ids"])
    if len(configured) > 50:
        raise ValueError(
            "Subagent configuration exceeds the maximum of 50 unique agents"
        )
    return result


def replace_graph_self(config: dict, previous: str, next_id: str) -> dict:
    """Expand source origin placeholders or rebind a duplicated graph's self."""
    result = copy.deepcopy(config)

    def replace(value):
        if isinstance(value, list):
            return [next_id if item == previous else item for item in value]
        return next_id if value == previous else value

    for edge in result.get("edges", []):
        edge["from"] = replace(edge["from"])
        if previous:
            edge["to"] = replace(edge["to"])
    if previous:
        # Duplication changes identity, including explicit self entry/result
        # and cycles that point back to the original Agent.
        for key in ("graphAgentIds", "entryAgentId", "resultAgentId"):
            if key in result:
                result[key] = replace(result[key])
    for graph in result.get("subagents", {}).get("graphs", []):
        for key in ("agent_ids", "entry_agent_id", "result_agent_id"):
            graph[key] = replace(graph[key])
        for edge in graph["edges"]:
            edge["from"] = replace(edge["from"])
            edge["to"] = replace(edge["to"])
        validate_subagent_topology(graph)
    return result


def clean_agent_config(payload: Any, *, create: bool = False) -> dict:
    obj = _require(payload, dict, "Agent configuration")
    result = {}
    # Only the authoring fields integrated in H1 are accepted here. The later
    # feature units will extend this allowlist with their own validators.
    nullable_strings = {
        "name",
        "description",
        "instructions",
        "model",
        "entryAgentId",
        "resultAgentId",
    }
    strings = {"provider", "category"}
    arrays = {"tools", "conversation_starters", "graphAgentIds"}
    for key in nullable_strings | strings | arrays:
        if key not in obj:
            continue
        value = obj[key]
        if value is None and key in nullable_strings:
            result[key] = None
        elif key in nullable_strings | strings:
            result[key] = _require(value, str, key)
        else:
            result[key] = _strings(value, key)
    if "provider" in result and not result["provider"].strip():
        raise ValueError("Agent provider is required")
    if "machine_permissions" in obj:
        from .machine_permissions import validate_access_map

        result["machine_permissions"] = validate_access_map(
            obj["machine_permissions"]
        )
    if "machine_default_permission" in obj:
        from .machine_permissions import agent_access_policy

        _, default = agent_access_policy(obj) or ({}, "write")
        result["machine_default_permission"] = default
    if "approval_mode" in obj:
        from .approval_settings import validate_mode

        result["approval_mode"] = validate_mode(
            obj["approval_mode"], inherit=True
        )
    if "model_parameters" in obj:
        result["model_parameters"] = copy.deepcopy(
            _require(obj["model_parameters"], dict, "model_parameters")
        )
    if "edges" in obj:
        result["edges"] = [
            clean_edge(edge) for edge in _require(obj["edges"], list, "edges")
        ]
    if "subagents" in obj:
        result["subagents"] = clean_subagents(obj["subagents"])
    if "recursion_limit" in obj:
        number = obj["recursion_limit"]
        if number is not None and (
            type(number) is not int or not 0 < number <= 2**53 - 1
        ):
            raise ValueError(
                "recursion_limit must be a positive safe integer or null"
            )
        result["recursion_limit"] = number
    if "maxHandoffs" in obj:
        limit = obj["maxHandoffs"]
        if type(limit) is not int or not 0 <= limit <= 2**53 - 1:
            raise ValueError("maxHandoffs must be a nonnegative safe integer")
        result["maxHandoffs"] = limit
    if create:
        if "provider" not in result or "model" not in result:
            raise ValueError("Agent provider and model are required")
        result.setdefault("tools", [])
    return result


def sanitize_model_parameters(params: dict) -> dict:
    """Port of agents/parameters.ts; numeric placeholders never reach models."""
    result = {}
    for key, value in params.items():
        if key not in _NUMERIC:
            result[key] = value
            continue
        number = None
        if type(value) in (int, float):
            number = value
        elif type(value) is str and value.strip():
            text = value.strip()
            with contextlib.suppress(ValueError):
                number = (
                    int(text, 0)
                    if text.lower().startswith(("0x", "0o", "0b"))
                    else float(text)
                )
        with contextlib.suppress(OverflowError):
            if number is not None and math.isfinite(number):
                result[key] = number
    return result
