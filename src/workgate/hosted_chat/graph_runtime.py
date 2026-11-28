"""LibreChat agents 4.0.1 MultiAgentGraph compiled with official Python LangGraph.

Source: src/graphs/MultiAgentGraph.ts createWorkflow, src/graphs/handoff.ts.
Pinned upstream graph_runtime.py blob 2f82f83339a77b41c3c6ae51b9344d2233aba577.
StateGraph owns supersteps, join barriers, Commands and Send fan-out.
A trusted Control callback binds each activation to the native Agent loop.
"""

import copy
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict, cast

from .agent_runtime import AgentSuspended, RuntimeSafetyError
from .agents import AgentRepository
from .approval_claims import ApprovalClaimStore
from .generation_actor_lease import ActorLease
from .graph_human_request import GraphHumanActionRequested, GraphHumanRequest
from .graph_member_scope import GraphMemberScope
from .graph_topology import (
    admit_agent_graph,
    build_graph_topology,
    endpoints,
    graph_members,
)
from .handoff import (
    PARENT,
    HandoffRouting,
    combine_handoff_commands,
    handoff_tools,
    is_transfer_tool,
    merge_handoff_state,
    receive_handoff,
)
from .message_reducers import agent_messages_reducer, messages_state_reducer
from .model_history import completed_model_steps
from .subagent_scope import (
    DetachedExecutionScope,
    SubagentGraphScope,
    SubagentScope,
)


def _merge_prompt_inputs(current: dict | None, updates: dict | None) -> dict:
    """Merge concurrent direct-edge prompts by destination, without cross-talk."""
    return {**(current or {}), **(updates or {})}


def _merge_public_proofs(
    current: dict[str, dict[str, str] | None] | None,
    updates: dict[str, dict[str, str] | None] | None,
) -> dict[str, dict[str, str] | None]:
    merged = dict(current or {})
    for identity, proof in (updates or {}).items():
        merged[identity] = (
            None if identity in merged and merged[identity] != proof else proof
        )
    return merged


def _step_digest(message: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            message, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _public_step_proofs(
    before: list[dict],
    messages: list[dict],
    updates: list[dict],
    *,
    visible: bool,
    current: list[dict],
) -> dict[str, str] | None:
    """Prove new public steps without duplicating their payloads in checkpoints."""
    if messages[: len(before)] != before:
        return None
    prior = {m.get("id") for m in [*before, *current]}
    proofs = {}
    for message in updates:
        identity = message.get("id")
        if not isinstance(identity, str) or identity in prior:
            return None
        if message.get("role") == "user" and message.get("source") == "steer":
            pass
        elif not visible:
            continue
        elif message.get("role") not in {"assistant", "tool"}:
            return None
        if identity in proofs:
            return None
        proofs[identity] = _step_digest(message)
    return proofs


def _public_graph_steps(outcome: dict) -> list[dict]:
    """Replay only hash-bound public graph steps in canonical graph order."""
    if outcome.get("publicHistoryReady") is not True:
        return []
    proofs = {}
    for activation in outcome.get("publicStepProofs", {}).values():
        if activation is None or proofs.keys() & activation.keys():
            return []
        proofs.update(activation)
    result = []
    for message in outcome["messages"]:
        identity = message.get("id")
        if identity in proofs:
            if _step_digest(message) != proofs.pop(identity):
                return []
            result.append(message)
    if proofs:
        return []
    try:
        return completed_model_steps([], result)
    except ValueError:
        return []


class MultiAgentState(TypedDict, total=False):
    messages: Annotated[list[dict], messages_state_reducer]
    agentMessages: Annotated[list[dict], agent_messages_reducer]
    promptInputs: Annotated[dict[str, list[dict]], _merge_prompt_inputs]
    subagentResult: dict | None
    manualSummary: str | None
    initialInputCount: int
    initialSystem: list[dict]
    handoffState: Annotated[dict | None, merge_handoff_state]
    publicHistoryReady: bool
    publicStepProofs: Annotated[
        dict[str, dict[str, str] | None], _merge_public_proofs
    ]


def _graph_imports():
    try:
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, Send
    except ImportError as exc:
        raise RuntimeError(
            "MultiAgentGraph needs the optional Control-only Hosted Chat extra; "
            "install workgate[hosted-chat] on Control"
        ) from exc
    from .graph_checkpoint import OwnedGraphCheckpointer

    return START, END, StateGraph, Command, Send, OwnedGraphCheckpointer


def _route_prompt(message: str) -> dict:
    return {
        "role": "user",
        "content": message,
        "additional_kwargs": {
            "role": "user",
            "isMeta": True,
            "source": "routing",
        },
    }


def _content(message: dict) -> str:
    value = message.get("content")
    return value if isinstance(value, str) else str(value or "")


def _limit_prompt(value: str, size: int = 64_000) -> str:
    return value[:size]


def _as_goto(commands: list[dict], *, Send: Any) -> list[Any]:
    result = []
    for command in commands:
        goto = command.get("goto")
        for item in goto if isinstance(goto, list) else [goto]:
            if isinstance(item, dict) and item.get("lg_name") == "Send":
                result.append(Send(item["node"], item["args"]))
            elif isinstance(item, str):
                result.append(item)
            else:
                raise ValueError("Unknown source Command destination")
    return result


@dataclass(frozen=True)
class GraphActivation:
    """One durable LangGraph task, with live owner-scoped member configuration."""

    agent: dict
    scope: GraphMemberScope | SubagentGraphScope
    history: list[dict]
    handoff_tools: list[dict]
    handoff_state: dict
    graph_root: dict | None = None
    visible_output: bool = True

    @property
    def activation_id(self) -> str:
        identity = self.scope.activation_id
        if identity is None:
            raise RuntimeSafetyError("Graph activation lacks member identity")
        return identity


@dataclass
class GraphMemberResult:
    messages: list[dict]
    commands: list[dict] = field(default_factory=list)


def _mark_graph_started(
    lease: ActorLease,
    namespace: str,
    fingerprint: str,
    *,
    stored: bool,
    team_scope: SubagentScope | DetachedExecutionScope | None = None,
) -> None:
    """Never resume an expired graph as a fresh sequence of native effects."""
    with lease.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        lease.assert_current(db)
        if team_scope is not None:
            team_scope.resolve(db, lease)
        row = db.execute(
            "SELECT state FROM runs WHERE id=?", (lease.run_id,)
        ).fetchone()
        state = json.loads(row["state"])
        markers = state.setdefault("ownedGraphCheckpoints", {})
        if namespace in markers:
            if markers[namespace] != fingerprint or not stored:
                raise RuntimeSafetyError(
                    "Graph checkpoint or definition is no longer available"
                )
            return
        if stored:
            raise RuntimeSafetyError(
                "Graph checkpoint has no matching run marker"
            )
        markers[namespace] = fingerprint
        db.execute(
            "UPDATE runs SET state=? WHERE id=?",
            (json.dumps(state, separators=(",", ":")), lease.run_id),
        )


async def run_multi_agent_graph(
    lease: ActorLease,
    root_agent_id: str,
    history: list[dict],
    *,
    execute_member: Callable[[GraphActivation], Awaitable[GraphMemberResult]],
    check_grant: Callable[[], None],
    recursion_limit: int,
    namespace: str = "foreground",
    result_only: bool = False,
    human_actions: bool = False,
    public_output: bool = False,
    team_scope: SubagentScope | DetachedExecutionScope | None = None,
    team_root: dict | None = None,
) -> list[dict]:
    """Execute the pinned workflow via a caller-authorized member callback.

    This private seam does not grant access to providers or native tools.
    The callback owns activation-scoped effect receipts and current grants.
    Public foreground mode returns only proven new public model steps.
    """
    if type(recursion_limit) is not int or not 0 < recursion_limit <= 2**31 - 1:
        raise ValueError("A finite positive graph recursion limit is required")
    if not namespace or "|" in namespace:
        raise ValueError("Invalid graph namespace")
    START, END, StateGraph, Command, Send, OwnedGraphCheckpointer = (
        _graph_imports()
    )
    from langchain_core.runnables import RunnableConfig
    from langgraph.config import get_config
    from langgraph.types import interrupt

    repository = AgentRepository(lease.database)
    claims = ApprovalClaimStore(lease.database)

    def check() -> None:
        lease.assert_current()
        check_grant()

    check()
    if (team_scope is None) != (team_root is None):
        raise PermissionError("Team graph requires an admitted definition")
    if team_scope is not None and human_actions:
        raise PermissionError("Team graph human actions are not enabled")
    if team_scope is not None and public_output:
        raise PermissionError("Private team cannot publish public output")
    if namespace == "foreground" and team_scope is None:
        with lease.database.read_snapshot() as db:
            lease.assert_current(db)
            row = db.execute(
                "SELECT agent_id FROM runs WHERE id=?", (lease.run_id,)
            ).fetchone()
            if row is None or row["agent_id"] != root_agent_id:
                raise PermissionError(
                    "Graph root does not match the admitted generation"
                )
    if team_scope is None:
        root = repository.get(lease.owner, root_agent_id)
        if root is None:
            raise PermissionError("Graph root Agent is not owned")
        topology = admit_agent_graph(lease.owner, root, repository)
    else:
        assert team_root is not None
        team_scope.assert_team_root(team_root)
        with lease.database.read_snapshot() as db:
            team_scope.resolve(db, lease)
        root = copy.deepcopy(team_root)
        identities = graph_members(root)
        if root_agent_id != team_scope.member_id or identities != [
            member for member, _ in team_scope.target_revisions
        ]:
            raise PermissionError(
                "Team graph membership does not match admission"
            )
        topology = build_graph_topology(root, identities)
    if topology is None or topology.default_agent_id is None:
        raise ValueError("Agent does not define a graph")
    entry = topology.default_agent_id
    members = topology.reachable_agent_ids or topology.agent_ids
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                root_agent_id,
                root["version"],
                topology.summary(),
                root.get("maxHandoffs"),
                root.get("resultAgentId"),
                recursion_limit,
                result_only,
                human_actions,
                *([True] if public_output else []),
            ],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    graph = StateGraph(MultiAgentState)
    handoff_sources = topology.handoff_sources
    destinations: dict[str, set[str]] = {ident: set() for ident in members}
    direct_destinations: dict[str, set[str]] = {
        ident: set() for ident in members
    }
    for edge in topology.edges:
        for src in endpoints(edge, "from"):
            if src not in members:
                continue
            for dest in endpoints(edge, "to"):
                if dest in members:
                    destinations[src].add(dest)
                    if edge in topology.direct_edges:
                        direct_destinations[src].add(dest)

    graph_scope = (
        SubagentGraphScope(team_scope) if team_scope is not None else None
    )
    saver = OwnedGraphCheckpointer(
        lease.database, lease.owner, actor_lease=lease, scope=graph_scope
    )
    graph_namespace = (
        graph_scope.graph_namespace
        if graph_scope is not None
        else lease.run_id + ":graph:" + namespace
    )
    checkpoint_config = saver.storage.config(
        saver.thread_id, graph_namespace, event_actor=True
    )
    stored = saver.get_tuple(cast(RunnableConfig, checkpoint_config))
    _mark_graph_started(
        lease,
        graph_namespace,
        fingerprint,
        stored=stored is not None,
        team_scope=team_scope,
    )
    source_history = messages_state_reducer([], copy.deepcopy(history))
    initial_system = [m for m in source_history if m.get("role") == "system"]
    initial_count = len(source_history)
    if stored is not None:
        values = stored.checkpoint["channel_values"]
        initial = (
            values
            if "initialInputCount" in values
            else values.get("__start__", {})
        )
        initial_count = initial["initialInputCount"]
        initial_system = copy.deepcopy(initial["initialSystem"])

    def live_member(agent_id: str) -> dict:
        check()
        current_root = repository.get(lease.owner, root_agent_id)
        if current_root is None or current_root["version"] != root["version"]:
            raise RuntimeSafetyError(
                "Graph definition changed during execution"
            )
        if team_scope is not None:
            with lease.database.read_snapshot() as db:
                team_scope.resolve(db, lease)
        member = repository.get(lease.owner, agent_id)
        if member is None:
            raise PermissionError("Graph member Agent was revoked")
        return member

    async def node(agent_id: str, state: MultiAgentState):
        activation_id = (get_config().get("configurable") or {}).get(
            "__pregel_task_id"
        )
        if not isinstance(activation_id, str) or not activation_id:
            raise RuntimeError(
                "LangGraph task activation is missing a durable identity"
            )
        member = live_member(agent_id)
        current = copy.deepcopy(state.get("messages", []))
        incoming = copy.deepcopy(state.get("agentMessages") or current)
        # Source handoff receipt recognition must ignore trailing user steers.
        transfer_history = list(current)
        trailing_steers = []
        while (
            transfer_history and transfer_history[-1].get("source") == "steer"
        ):
            trailing_steers.insert(0, transfer_history.pop())
        reception = receive_handoff(transfer_history, agent_id)
        if reception is not None:
            incoming = [
                message
                for message in reception["filteredMessages"]
                if message.get("role") != "system"
            ]
            incoming.extend(copy.deepcopy(trailing_steers))
            prompt = reception.get("instructions")
            if prompt:
                if incoming and incoming[-1].get("role") == "tool":
                    incoming.append(
                        {
                            "role": "assistant",
                            "content": f"[Processed tool result and transferring to {agent_id}]",
                        }
                    )
                incoming.append(_route_prompt(_limit_prompt(prompt)))
        elif (state.get("promptInputs") or {}).get(agent_id):
            incoming = copy.deepcopy(state.get("promptInputs", {})[agent_id])
        # Member-specific instructions, never another member's system prompt.
        initial_history = state.get("initialSystem", initial_system)
        summary_carriers = [
            copy.deepcopy(m)
            for m in initial_history
            if m.get("role") == "system"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("<summary>\n")
        ]
        root_instructions = [
            m["content"]
            for m in initial_history
            if m.get("role") == "system"
            and isinstance(m.get("content"), str)
            and not m["content"].startswith("<summary>\n")
        ]
        incoming = [m for m in incoming if m.get("role") != "system"]
        instructions = (
            "\n\n".join(root_instructions)
            if agent_id == root_agent_id and root_instructions
            else member.get("instructions")
        )
        if instructions:
            incoming.insert(0, {"role": "system", "content": instructions})
        incoming[1 if instructions else 0 : 1 if instructions else 0] = (
            summary_carriers
        )
        counter = iter(range(2**31))

        def make_id() -> str:
            return f"graph:{activation_id}:{next(counter)}"

        incoming = messages_state_reducer([], incoming, id_factory=make_id)
        before = copy.deepcopy(incoming)
        route = HandoffRouting(
            entry, root.get("maxHandoffs"), topology.routing_parallel
        )
        route.restore(state.get("handoffState"))
        tools = handoff_tools(
            lease.owner, root, repository, source_agent_id=agent_id
        )
        activation = GraphActivation(
            member,
            SubagentGraphScope(team_scope, agent_id, activation_id)
            if team_scope is not None
            else GraphMemberScope(
                graph_namespace,
                root_agent_id,
                root["version"],
                agent_id,
                activation_id,
            ),
            incoming,
            tools,
            route.snapshot(),
            copy.deepcopy(root) if team_scope is not None else None,
            visible_output=not (
                root.get("hide_sequential_outputs") is True
                and root.get("resultAgentId") is not None
                and agent_id != root["resultAgentId"]
                and not topology.routing_parallel
            ),
        )
        if human_actions:
            assert isinstance(activation.scope, GraphMemberScope)
            # Replay prior SDK interrupt positions even when the member loop
            # has already advanced beyond the reviewed edge.
            for prior in claims.resolved_graph_interrupts(
                lease, activation.scope
            ):
                if interrupt(prior["request"]) != prior["actionId"]:
                    raise RuntimeSafetyError(
                        "Graph resume does not match its durable human claim"
                    )
        try:
            result = await execute_member(activation)
        except GraphHumanActionRequested as error:
            if not human_actions:
                raise
            if error.request.scope != activation.scope:
                raise RuntimeSafetyError(
                    "Human action belongs to another graph activation"
                ) from error
            interrupt(error.request.value())
            raise RuntimeSafetyError(
                "Graph resumed without matching member review evidence"
            ) from error
        live_member(agent_id)
        messages = messages_state_reducer(
            [], result.messages, id_factory=make_id
        )
        diff = (
            messages[len(before) :]
            if messages[: len(before)] == before
            else messages
        )
        diff = [message for message in diff if message.get("role") != "system"]
        update: dict = {"messages": diff, "agentMessages": []}
        if root.get("resultAgentId") == agent_id:
            replies = [m for m in diff if m.get("role") in {"assistant", "ai"}]
            update["subagentResult"] = {
                "agentId": agent_id,
                "message": replies[-1] if replies else None,
            }

        def record_public_steps() -> None:
            if public_output:
                update["publicStepProofs"] = {
                    activation_id: _public_step_proofs(
                        before,
                        messages,
                        update["messages"],
                        visible=activation.visible_output,
                        current=current,
                    )
                }

        if result.commands:
            # The callback returns original transfer Commands. Recheck each
            # source/destination and scope before the SDK schedules recipients.
            commands = copy.deepcopy(result.commands)
            allowed = {
                (dest, edge.get("handoffScope") or "turn")
                for edge in topology.handoff_edges
                if agent_id in endpoints(edge, "from")
                for dest in endpoints(edge, "to")
            }
            for command in commands:
                target = command.get("goto")
                request = (command.get("update") or {}).get(
                    "handoffRequest"
                ) or {}
                if (
                    command.get("graph") != PARENT
                    or not isinstance(target, str)
                    or target not in members
                    or (target, request.get("scope")) not in allowed
                    or request.get("sourceAgentId") != agent_id
                    or request.get("targetAgentId") != target
                    or not isinstance(request.get("toolCallId"), str)
                    or not request["toolCallId"]
                ):
                    raise PermissionError(
                        "Graph transfer is not declared for this activation"
                    )
                live_member(target)
            commands = combine_handoff_commands(
                commands,
                parallel_batch=hashlib.sha256(
                    activation_id.encode()
                ).hexdigest()[:8],
            )
            route.finalize(
                commands,
                {"messages": before, "handoffState": route.snapshot()},
                {"configurable": {"checkpoint_ns": activation_id}},
            )
            update["handoffState"] = route.snapshot()
            # Transfer acknowledgements belong to the shared history exactly
            # once; parallel Send payloads carry their own recipient history.
            # A provider may reuse an earlier call ID. Only deduplicate
            # receipts from the current final proposal, not prior turns.
            seen_tools = set()
            for message in reversed(diff):
                if message.get("role") in {"assistant", "ai"}:
                    break
                if message.get("role") == "tool":
                    seen_tools.add(message.get("tool_call_id"))
            applied_steers = [
                m
                for m in diff
                if m.get("source") == "steer"
                and isinstance(m.get("steerId"), str)
            ]
            if applied_steers:
                update["messages"] = [
                    m for m in diff if m not in applied_steers
                ]
            for command in commands:
                targets = command.get("goto")
                payloads = (
                    [
                        item["args"]
                        for item in targets
                        if isinstance(item, dict)
                        and item.get("lg_name") == "Send"
                    ]
                    if isinstance(targets, list)
                    else [command.get("update") or {}]
                )
                for payload in payloads:
                    # The source builder ends each transfer payload with
                    # its acknowledgement. Prior payload history is not a
                    # new receipt and must not be re-appended to shared state.
                    payload_messages = payload.get("messages") or []
                    if payload_messages:
                        message = payload_messages[-1]
                        identity = message.get("tool_call_id")
                        name = message.get("name")
                        if (
                            message.get("role") == "tool"
                            and isinstance(name, str)
                            and is_transfer_tool(name)
                            and isinstance(identity, str)
                            and identity not in seen_tools
                        ):
                            seen_tools.add(identity)
                            receipt = copy.deepcopy(message)
                            receipt.setdefault(
                                "id",
                                f"handoff_result:{activation_id}:{identity}",
                            )
                            update["messages"].append(receipt)
                    if isinstance(targets, list):
                        existing = {
                            m.get("steerId")
                            for m in payload.get("messages", [])
                        }
                        payload.setdefault("messages", []).extend(
                            copy.deepcopy(
                                [
                                    m
                                    for m in applied_steers
                                    if m["steerId"] not in existing
                                ]
                            )
                        )
                        payload["initialSystem"] = initial_history
                        payload["initialInputCount"] = state.get(
                            "initialInputCount", initial_count
                        )
            update["messages"].extend(copy.deepcopy(applied_steers))
            record_public_steps()
            goto = _as_goto(commands, Send=Send)
            return Command(
                update=update,
                goto=goto[0]
                if len(goto) == 1 and not isinstance(goto[0], Send)
                else goto,
            )
        record_public_steps()
        if agent_id in handoff_sources and direct_destinations[agent_id]:
            direct = list(
                dict.fromkeys(
                    dest
                    for edge in topology.direct_edges
                    if agent_id in endpoints(edge, "from")
                    for dest in endpoints(edge, "to")
                    if dest in members
                )
            )
            return Command(update=update, goto=direct)
        return update

    for agent_id in members:

        async def wrapper(state: MultiAgentState, _agent_id=agent_id):
            return await node(_agent_id, state)

        graph.add_node(
            agent_id,
            wrapper,
            destinations=tuple(destinations[agent_id]) + (END,),
        )

    for agent_id in topology.starting_nodes:
        if agent_id in members:
            graph.add_edge(START, agent_id)

    groups: dict[str, list[dict]] = {}
    for edge in topology.direct_edges:
        for target in endpoints(edge, "to"):
            if target in members:
                groups.setdefault(target, []).append(edge)
    for target, edges in groups.items():
        prompted = [edge for edge in edges if edge.get("prompt")]
        to_node = target
        if prompted:
            to_node = f"fan_in_{target}_prompt"
            prompt, exclude = (
                prompted[0].get("prompt"),
                prompted[0].get("excludeResults"),
            )

            async def fan_in(
                state: MultiAgentState,
                _prompt=prompt,
                _exclude=exclude,
                _target=target,
            ):
                if not isinstance(_prompt, str):
                    raise ValueError(
                        "Persisted direct-edge prompt must be a string"
                    )
                use_results = "{results}" in _prompt
                source_count = state.get("initialInputCount", initial_count)
                rendered = _prompt.replace(
                    "{results}",
                    _limit_prompt(
                        "\n".join(
                            _content(message)
                            for message in state.get("messages", [])[
                                source_count:
                            ]
                        )
                    ),
                )
                if not rendered:
                    return {}
                prompt_msg = _route_prompt(_limit_prompt(rendered))
                if _exclude is True or (use_results and _exclude is not False):
                    # Concurrent B/C prompt branches must never share a single
                    # last-value agentMessages channel at the join barrier.
                    return {
                        "messages": [prompt_msg],
                        "promptInputs": {
                            _target: messages_state_reducer(
                                state.get("messages", [])[:source_count],
                                [prompt_msg],
                            )
                        },
                    }
                return {"messages": [prompt_msg]}

            graph.add_node(to_node, fan_in)
            graph.add_edge(to_node, target)
        for edge in edges:
            sources = [
                source
                for source in endpoints(edge, "from")
                if source in members
            ]
            if not prompted:
                sources = [
                    source
                    for source in sources
                    if source not in handoff_sources
                ]
            if sources:
                graph.add_edge(
                    sources if len(sources) > 1 else sources[0], to_node
                )

    compiled = graph.compile(checkpointer=saver, name="multi_agent_graph")
    initial = (
        None
        if stored is not None
        else {
            "messages": source_history,
            "agentMessages": [],
            "initialInputCount": initial_count,
            **(
                {"publicHistoryReady": True, "publicStepProofs": {}}
                if public_output
                else {}
            ),
            "initialSystem": copy.deepcopy(initial_system),
            "handoffState": HandoffRouting(
                entry, root.get("maxHandoffs"), topology.routing_parallel
            ).snapshot(),
        }
    )
    config = cast(
        RunnableConfig,
        {**checkpoint_config, "recursion_limit": recursion_limit},
    )

    async def pending_interrupts():
        snapshot = await compiled.aget_state(config)
        return [item for task in snapshot.tasks for item in task.interrupts]

    if stored is not None and human_actions:
        resume = {}
        for item in await pending_interrupts():
            request = GraphHumanRequest.parse(item.value)
            if request.scope.graph_namespace != graph_namespace:
                raise RuntimeSafetyError(
                    "Graph interrupt belongs to another graph"
                )
            for prior in claims.resolved_graph_interrupts(lease, request.scope):
                if prior["id"] == item.id and prior["request"] == item.value:
                    resume[item.id] = prior["actionId"]
        if resume:
            initial = Command(resume=resume)
    outcome = await compiled.ainvoke(initial, config)
    check()
    if outcome.get("__interrupt__"):
        if not human_actions:
            raise RuntimeSafetyError("Graph human actions are not enabled")
        persisted = await pending_interrupts()
        item = min(outcome["__interrupt__"], key=lambda value: value.id)
        if not any(
            saved.id == item.id and saved.value == item.value
            for saved in persisted
        ):
            raise RuntimeSafetyError("Graph interruption was not durably saved")
        request = GraphHumanRequest.parse(item.value)
        if request.scope.graph_namespace != graph_namespace:
            raise RuntimeSafetyError("Graph interrupt belongs to another graph")
        claims.publish_graph_interrupt(lease, item.id, request)
        raise AgentSuspended("Waiting for graph member human action")
    if public_output:
        return _public_graph_steps(outcome)
    if result_only and root.get("resultAgentId"):
        result = outcome.get("subagentResult") or {}
        message = (
            result.get("message")
            if result.get("agentId") == root["resultAgentId"]
            else None
        )
        return [message] if isinstance(message, dict) else []
    return outcome["messages"]
