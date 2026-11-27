"""LibreChat 0.8.8 / agents 4.0.1 isolated subagent tool adapter.

Ports buildSubagentConfigs, buildSubagentToolParams and filterSubagentResult.
Private configuration only; no tool advertisement or child execution.
Pinned upstream blob f36dc5d6e17ed63bb79de6c6eb145c76ce03012e.
Detached tasks and child-thread continuation are not advertised without their
host store, matching the upstream capability-gated schema.
"""

import copy
import math
from dataclasses import dataclass
from typing import Any

from .agent_validation import clean_subagents
from .database import SQLiteDatabase

SUBAGENT_TOOL_NAME = "subagent"
MAX_SUBAGENT_DEPTH = 5
MAX_SUBAGENT_RUN_CONFIGS = 100
SUBAGENT_DESCRIPTION = "Delegate a task to a specialized subagent or bounded agent team that runs in an isolated context window. The delegated execution returns only its designated final text result — all intermediate tool calls, reasoning, and context stay isolated.\n\nWHEN TO USE:\n- The task is self-contained and can be described in a single prompt.\n- You want to offload verbose or exploratory work without bloating your own context.\n- A specialized subagent is available for the task domain.\n\nWHAT HAPPENS:\n- A fresh agent or configured agent graph is created with the task description as its only input, unless the host enables child-thread continuation and you provide a saved thread id.\n- The delegated agent or team runs to completion using isolated tools and context.\n- Only the single agent's final response or the graph's designated result-agent response is returned to you.\n\nCONSTRAINTS:\n- subagent_type must match one of the available types listed below.\n- The subagent cannot see your conversation history."
_INTENT_DESCRIPTION = (
    "ALWAYS write this field FIRST, before any other argument. One present-progressive "
    'sentence saying what THIS call is about to do: "Searching for OAuth handling '
    "in the callback router\". Shown to the user as this call's live status. "
    "Never name the tool. Sibling calls to one tool must differ."
)
_DESCRIPTION = "Complete task description for the subagent. This is the ONLY information it receives — include all necessary context, requirements, and constraints."
_TYPE_DESCRIPTION = (
    "Which subagent type to delegate to. Must be one of the available types."
)
_BACKGROUND_DESCRIPTION = "Set true to start the subagent as a detached process-local task and return a background_task_id immediately. Poll the host background-task tool to collect its result. The task can outlive this turn but does not survive a process restart."
_THREAD_DESCRIPTION = "Continue a host-owned child thread using a fresh execution lease. The saved thread must belong to this scope and subagent type. Only available with run_in_background."


@dataclass(frozen=True)
class SubagentConfig:
    type: str
    name: str
    description: str
    agent: dict
    max_turns: int | None
    owner: str
    self_spawn: bool = False
    allow_nested: bool = False
    children: tuple[SubagentConfig, ...] = ()
    ordinary_chat: bool = False


def chat_subagent_config(owner: str) -> SubagentConfig:
    """Ordinary model chat delegates to itself without creating a saved Agent."""
    return SubagentConfig(
        "self",
        "self",
        "Run a focused subtask with the current model and chat permissions in an isolated context.",
        {},
        None,
        owner,
        self_spawn=True,
        ordinary_chat=True,
    )


def resolve_subagent_max_turns(
    agent: dict, endpoint_config: dict | None = None
) -> int | None:
    """Only an explicit agent or endpoint setting limits child turns."""
    endpoint = endpoint_config or {}
    limit: Any = endpoint.get("recursionLimit")
    if type(limit) not in (int, float) or not 0 < limit <= 2**53 - 1:
        limit = None
    override: Any = agent.get("recursion_limit")
    if type(override) in (int, float) and 0 < override <= 2**53 - 1:
        limit = override
    cap: Any = endpoint.get("maxRecursionLimit")
    if (
        type(cap) in (int, float)
        and 0 < cap <= 2**53 - 1
        and (limit is None or limit > cap)
    ):
        limit = cap
    return math.floor(limit / 3) if limit is not None else None


def build_subagent_configs(
    owner: str,
    parent: dict | None,
    database: SQLiteDatabase,
    *,
    endpoint_config: dict | None = None,
    ancestors: tuple[str, ...] = (),
    depth: int = 0,
    _count: list[int] | None = None,
) -> list[SubagentConfig]:
    """Source run.ts self/default, explicit children, ancestry and expansion bounds."""
    from .agents import AgentRepository

    if parent is None:
        return []
    settings = parent.get("subagents")
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        return []
    if depth > MAX_SUBAGENT_DEPTH:
        raise ValueError(
            f"Subagent graph exceeds the maximum depth of {MAX_SUBAGENT_DEPTH} at agent {parent.get('id')}."
        )
    settings = clean_subagents(settings)
    count = _count if _count is not None else [0]
    repository = AgentRepository(database)
    configs = []
    if parent.get("author") is not None and parent["author"] != owner:
        raise PermissionError(
            "Subagent parent is not owned by the current principal"
        )

    def append(agent: dict, kind: str, self_spawn: bool = False) -> None:
        count[0] += 1
        if count[0] > MAX_SUBAGENT_RUN_CONFIGS:
            raise ValueError(
                f"Subagent run configuration exceeds the maximum of {MAX_SUBAGENT_RUN_CONFIGS} expanded entries."
            )
        name = agent.get("name")
        if self_spawn:
            name = name if name is not None else "self"
            description = f"Spawn {name} in an isolated context to handle a focused subtask. Verbose tool output stays in the child's context; only a summary returns."
        else:
            name = name if name is not None else agent["id"]
            description = agent.get("description")
            if description is None:
                description = f"Delegate a subtask to the {name} agent in an isolated context."
        child = copy.deepcopy(agent)
        # run.ts buildAgentInput excludes Ask User in every child scope.
        child["tools"] = [
            tool
            for tool in child.get("tools", [])
            if tool != "ask_user_question"
        ]
        for key in (
            "initialSummary",
            "discoveredTools",
            "initial_summary",
            "discovered_tools",
        ):
            child.pop(key, None)
        children = []
        if self_spawn:
            child.pop("subagents", None)
        else:
            if depth + 1 > MAX_SUBAGENT_DEPTH:
                raise ValueError(
                    f"Subagent graph exceeds the maximum depth of {MAX_SUBAGENT_DEPTH} at agent {agent['id']}."
                )
            children = build_subagent_configs(
                owner,
                child,
                database,
                endpoint_config=endpoint_config,
                ancestors=(*ancestors, str(parent.get("id") or "")),
                depth=depth + 1,
                _count=count,
            )
        configs.append(
            SubagentConfig(
                kind,
                str(name),
                str(description),
                child,
                resolve_subagent_max_turns(agent, endpoint_config),
                owner,
                self_spawn=self_spawn,
                allow_nested=not self_spawn,
                children=tuple(children),
            )
        )

    if settings.get("allowSelf") is not False:
        append(parent, "self", True)
    for identity in settings.get("agent_ids", []):
        if identity == parent.get("id") or identity in ancestors:
            continue
        child = repository.get(owner, identity)
        if child is None:
            raise PermissionError(
                "Subagent is not owned by the current principal"
            )
        append(child, identity)
    # LibreChat packages/api/src/agents/run.ts buildSubagentConfigs: graph entries
    # are isolated StateGraphs with an explicitly designated result member.
    for definition in settings.get("graphs") or []:
        members = []
        for identity in definition["agent_ids"]:
            member = repository.get(owner, identity)
            if member is None:
                raise PermissionError(
                    "Graph subagent member is not owned by the current principal"
                )
            members.append(member)
        count[0] += 1
        if count[0] > MAX_SUBAGENT_RUN_CONFIGS:
            raise ValueError(
                f"Subagent run configuration exceeds the maximum of {MAX_SUBAGENT_RUN_CONFIGS} expanded entries."
            )
        entry = next(
            (
                member
                for member in members
                if member["id"] == definition["entry_agent_id"]
            ),
            None,
        )
        if entry is None:
            raise ValueError("Graph subagent entry is not a configured member")
        team = copy.deepcopy(entry)
        team["tools"] = [
            tool
            for tool in team.get("tools", [])
            if tool != "ask_user_question"
        ]
        for key in (
            "initialSummary",
            "discoveredTools",
            "initial_summary",
            "discovered_tools",
            "subagents",
        ):
            team.pop(key, None)
        team["edges"] = [copy.deepcopy(edge) for edge in definition["edges"]]
        # The upstream graph receives every member, even a non-edge member.
        team["graphAgentIds"] = [member["id"] for member in members]
        team["entryAgentId"] = definition["entry_agent_id"]
        team["resultAgentId"] = definition["result_agent_id"]
        for edge in team["edges"]:
            if edge.get("excludeResults") is False and not edge.get("prompt"):
                edge.pop("excludeResults")
        configs.append(
            SubagentConfig(
                definition["type"],
                definition["name"],
                definition["description"],
                team,
                min(
                    (
                        turns
                        for member in members
                        if (
                            turns := resolve_subagent_max_turns(
                                member, endpoint_config
                            )
                        )
                        is not None
                    ),
                    default=None,
                ),
                owner,
            )
        )
    if len({config.type for config in configs}) != len(configs):
        raise ValueError("Duplicate subagent type")
    return configs


def build_subagent_tool_params(
    configs: list[SubagentConfig],
    *,
    background: bool = False,
    thread_continuation: bool = False,
) -> dict:
    """Exact agents/src/tools/SubagentTool.ts dynamic model-facing schema."""
    types = [config.type for config in configs]
    properties = {
        "intent": {"type": "string", "description": _INTENT_DESCRIPTION},
        "description": {"type": "string", "description": _DESCRIPTION},
        "subagent_type": {
            "type": "string",
            "enum": types,
            "description": _TYPE_DESCRIPTION
            + " Available: "
            + ", ".join(types)
            + ".",
        },
    }
    description = SUBAGENT_DESCRIPTION
    if background:
        properties["run_in_background"] = {
            "type": "boolean",
            "description": _BACKGROUND_DESCRIPTION,
        }
        description += "\n\nBACKGROUND EXECUTION:\n- Set run_in_background to true when you do not need the result immediately. The call returns a background_task_id; use the host background-task tools to poll, steer, queue, interrupt, or cancel it."
        if thread_continuation:
            properties["subagent_thread_id"] = {
                "type": "string",
                "description": _THREAD_DESCRIPTION,
            }
            description += "\n- To continue a completed child later, set run_in_background and pass its subagent_thread_id. The host starts a fresh execution from saved history; it does not restore a completed runtime."
    description += "\n\nAvailable types:\n" + "\n".join(
        f'- "{config.type}" ({config.name}): {config.description}'
        for config in configs
    )
    return {
        "name": SUBAGENT_TOOL_NAME,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": ["description", "subagent_type"],
        },
        "description": description,
    }


def subagent_tools(
    configs: list[SubagentConfig],
    *,
    background: bool = False,
    thread_continuation: bool = False,
) -> list[dict]:
    if not configs:
        return []
    params = build_subagent_tool_params(
        configs, background=background, thread_continuation=thread_continuation
    )
    return [
        {
            "type": "function",
            "function": {
                "name": params["name"],
                "description": params["description"],
                "parameters": params["schema"],
            },
        }
    ]


def filter_subagent_result(messages: list[dict]) -> str:
    """Source SubagentExecutor.filterSubagentResult; final AI text only."""
    for message in reversed(messages):
        if message.get("role") not in {"assistant", "ai"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content:
                return content
            continue
        if not isinstance(content, list):
            continue
        parts, deltas = [], []
        delta_index = None

        def flush(parts=parts, deltas=deltas) -> None:
            nonlocal delta_index
            if deltas:
                parts.append("".join(deltas))
                deltas.clear()
                delta_index = None

        for block in content:
            if isinstance(block, str):
                flush()
                if block:
                    parts.append(block)
                continue
            if not isinstance(block, dict):
                flush()
                continue
            kind = block.get("type")
            text = block.get("text")
            text = (
                text
                if kind in {"text", "text_delta"} and isinstance(text, str)
                else ""
            )
            if kind == "text_delta":
                if not text:
                    continue
                index = block.get("index")
                index = index if type(index) in (str, int, float) else None
                if (
                    delta_index is not None
                    and index is not None
                    and index != delta_index
                ):
                    flush()
                if delta_index is None:
                    delta_index = index
                deltas.append(text)
                continue
            if kind == "text" and not text:
                continue
            flush()
            if text:
                parts.append(text)
        flush()
        if parts:
            return "\n".join(parts)
    return "Task completed"
