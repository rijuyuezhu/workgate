"""Private native tool node for the checkpointed Hosted Chat Agent loop."""

import copy
import json
import time
from collections.abc import Awaitable, Callable, Collection
from typing import Protocol

from ..hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    AgentSuspended,
    ToolBatchOutput,
)
from ..hosted_chat.agents import AgentRepository
from ..hosted_chat.approval_claims import ApprovalClaimStore
from ..hosted_chat.approval_settings import call_decision, effective_mode
from ..hosted_chat.ask_user_tools import (
    ASK_USER_QUESTION_TOOL_NAME,
    ASK_USER_TOOLS,
    ask_user_requested,
    build_ask_user_payload,
)
from ..hosted_chat.detached_task_controls import CHECK_BACKGROUND_TASK
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.graph_human_request import (
    GraphHumanActionRequested,
    GraphHumanRequest,
)
from ..hosted_chat.graph_runtime import GraphActivation
from ..hosted_chat.handoff import (
    build_handoff_command,
    handoff_edge_for_call,
    handoff_tools,
    is_transfer_tool,
)
from ..hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from ..hosted_chat.loop_receipts import execute_checkpointed_batch
from ..hosted_chat.machine_permissions import effective_machine_access
from ..hosted_chat.mcp_projection import native_tool_history_message
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..hosted_chat.storage import WorkspaceRepository
from ..hosted_chat.subagent_scope import (
    DetachedExecutionScope,
    SubagentGraphScope,
    SubagentScope,
    detached_root_scope,
)
from ..hosted_chat.subagents import SUBAGENT_TOOL_NAME
from .hosted_chat_native_creation import _grant, _registered
from .hosted_chat_native_dispatch import NativeToolDispatcher, _selected_tools
from .runtime import ControlRuntime

_DELEGATION_TOOLS = frozenset({SUBAGENT_TOOL_NAME, CHECK_BACKGROUND_TASK})


class Delegation(Protocol):
    def definitions(self) -> list[dict]: ...
    def validate(self, call: ModelToolCall) -> None: ...
    def authorize_receipt(
        self, node: NativeLoopToolNode, call: ModelToolCall, record: dict
    ) -> None: ...
    async def invoke(
        self,
        node: NativeLoopToolNode,
        call: ModelToolCall,
        context: AgentContext,
        authorize_effects: Callable[
            [list[ModelToolCall], AgentLoopState], Awaitable[None]
        ]
        | None,
    ) -> tuple[dict, dict]: ...


class NativeLoopToolNode:
    """Compose native dispatch and durable replay without a generic MCP fallback.

    The foreground caller must supply the live principal/grant check and run
    the content-policy hook before executing native effects. Approval claims
    are evaluated against the live owner settings inside the receipt edge.
    """

    def __init__(
        self,
        runtime: ControlRuntime,
        repository: DurableTaskRepository,
        checkpoint: OwnedLoopCheckpoint,
        *,
        allowed_tools: Collection[str],
        scopes: set[str],
        expires: int | None,
        task_machine: str | None,
        check_grant: Callable[[], None],
        activation: GraphActivation | None = None,
        delegation: Delegation | None = None,
    ) -> None:
        self.runtime = runtime
        self.repository = repository
        self.checkpoint = checkpoint
        self.allowed_tools = frozenset(allowed_tools)
        self.scopes = frozenset(scopes)
        self.expires = expires
        self.task_machine = task_machine
        self._check_grant = check_grant
        self.activation = activation
        self.delegation = delegation
        if (
            (activation is not None and activation.scope != checkpoint.scope)
            or repository.scope != checkpoint.scope
            or repository.database.path != checkpoint.lease.database.path
            or repository.database.root
            != runtime.config.state_dir / "hosted-chat"
        ):
            raise PermissionError(
                "Native node does not belong to checkpoint database"
            )

        detached = detached_root_scope(checkpoint.scope)
        if detached is not None and task_machine != (
            detached.disclosure_access or {}
        ).get("task_machine"):
            raise PermissionError(
                "Detached task machine differs from admission"
            )
        self.native = NativeToolDispatcher(runtime)

    def current_grant(self) -> tuple[set[str], int | None]:
        self.checkpoint.assert_current()
        self._check_grant()
        scopes, expires = _grant(
            self.runtime,
            self.checkpoint.lease.owner,
            set(self.scopes),
            self.expires,
        )
        if expires is not None and expires <= time.time():
            raise PermissionError("Native tool grant expired")
        if not self.scopes.issubset(scopes):
            raise PermissionError("Native tool principal grant was reduced")
        return self.repository.cap_grant(scopes, expires)

    def _question_enabled(self, agent: dict | None) -> bool:
        # For ephemeral turns the trusted caller supplies the opt-in through
        # allowed_tools. Saved Agents must also retain their explicit selection.
        if (
            isinstance(self.checkpoint.scope, DetachedExecutionScope)
            or self.checkpoint.lease.wakeup_task_id is not None
            or ASK_USER_QUESTION_TOOL_NAME not in self.allowed_tools
        ):
            return False
        if self.checkpoint.scope is None:
            return agent is None or ask_user_requested(agent, {})
        if self.activation is None or isinstance(
            self.checkpoint.scope, SubagentGraphScope
        ):
            return False
        _, agents = self.repository.tool_context_chain(self.checkpoint.lease)
        return all(
            item is not None and ask_user_requested(item, {}) for item in agents
        )

    @staticmethod
    def _validate_question_batch(calls: list[ModelToolCall]) -> None:
        if any(call.name == ASK_USER_QUESTION_TOOL_NAME for call in calls):
            if len(calls) != 1:
                raise ValueError(
                    "Ask User cannot run in parallel with another tool"
                )
            build_ask_user_payload(calls[0].arguments, calls[0].call_id)

    def _handoffs(self) -> tuple[dict, list[dict]]:
        if self.activation is None:
            return {}, []
        self.current_grant()
        lease = self.checkpoint.lease
        agents = AgentRepository(lease.database)
        scope = self.activation.scope
        if isinstance(scope, SubagentGraphScope):
            root = self.activation.graph_root
            if root is not None:
                scope.team.assert_team_root(root)
        else:
            root = agents.get(lease.owner, scope.root_id)
        if root is None:
            raise PermissionError("Graph root Agent is no longer owned")
        tools = handoff_tools(
            lease.owner,
            root,
            agents,
            source_agent_id=self.activation.scope.member_id,
        )
        if tools != self.activation.handoff_tools:
            raise PermissionError(
                "Graph handoff definitions changed during activation"
            )
        return root, tools

    def _transfer(self, call: ModelToolCall, messages: list[dict]) -> dict:
        root, tools = self._handoffs()
        if self.activation is None or self.activation.scope.member_id is None:
            raise PermissionError("Handoff requires a graph activation")
        definition = next(
            (
                tool["function"]
                for tool in tools
                if tool["function"]["name"] == call.name
            ),
            None,
        )
        if definition is None:
            raise PermissionError(
                "Handoff tool is not declared for this activation"
            )
        schema = definition["parameters"]
        if (
            set(call.arguments) - set(schema["properties"])
            or any(
                key not in call.arguments for key in schema.get("required", [])
            )
            or any(
                not isinstance(value, str) for value in call.arguments.values()
            )
        ):
            raise ValueError("Invalid handoff arguments")
        lease = self.checkpoint.lease
        edge = handoff_edge_for_call(
            lease.owner,
            root,
            AgentRepository(lease.database),
            call.name,
            source_agent_id=self.activation.scope.member_id,
        )
        command = build_handoff_command(
            edge,
            self.activation.scope.member_id,
            self.activation.agent.get("name")
            or self.activation.scope.member_id,
            call.name,
            call.arguments,
            call.call_id,
            messages,
        )
        if command is None:
            raise ValueError("Conditional handoff did not select a destination")
        return command

    def _prepare(self, calls: list[ModelToolCall]) -> list[ModelToolCall]:
        # Validate every transfer and native target before releasing siblings.
        for call in calls:
            if is_transfer_tool(call.name):
                self._transfer(call, [])
            elif call.name in _DELEGATION_TOOLS:
                if self.delegation is None:
                    raise PermissionError("Foreground delegation is disabled")
                self.delegation.validate(call)
        self.native.prepare_model_batch(
            self.repository,
            self.checkpoint.lease,
            [
                call
                for call in calls
                if not is_transfer_tool(call.name)
                and call.name
                not in ({ASK_USER_QUESTION_TOOL_NAME} | _DELEGATION_TOOLS)
            ],
            allowed_tools=self.allowed_tools,
            task_machine=self.task_machine,
        )
        return calls

    async def refresh_tools(self, state: AgentLoopState) -> None:
        scopes, expires = self.current_grant()
        state.context.tools = await self.native.definitions(
            self.repository,
            self.checkpoint.lease,
            allowed_tools=self.allowed_tools,
            scopes=scopes,
            expires=expires,
            task_machine=self.task_machine,
        )
        self.current_grant()
        _, agent = self.repository.tool_context(self.checkpoint.lease)
        if self._question_enabled(agent):
            state.context.tools.extend(copy.deepcopy(ASK_USER_TOOLS))
        state.context.tools.extend(copy.deepcopy(self._handoffs()[1]))
        if self.delegation is not None:
            state.context.tools.extend(self.delegation.definitions())

    def preflight(
        self, calls: list[ModelToolCall], state: AgentLoopState
    ) -> list[ModelToolCall]:
        self.current_grant()
        calls = copy.deepcopy(complete_tool_batch(calls))
        shown = {tool["function"]["name"] for tool in state.context.tools}
        if any(call.name not in shown for call in calls):
            raise PermissionError("Native tool was not advertised")
        self._validate_question_batch(calls)
        if calls and calls[0].name == ASK_USER_QUESTION_TOOL_NAME:
            _, agent = self.repository.tool_context(self.checkpoint.lease)
            if not self._question_enabled(agent):
                raise PermissionError("Ask User is no longer selected")
            return calls
        return self._prepare(calls)

    def _decision(self, call: ModelToolCall) -> dict:
        lease = self.checkpoint.lease
        conversation, agents = self.repository.tool_context_chain(
            lease, authorization=True
        )
        repo = WorkspaceRepository(self.repository.database)
        mode = None
        for agent in agents:
            mode = effective_mode(
                repo, lease.owner, conversation, agent, parent_mode=mode
            )
        assert mode is not None
        endpoints = repo.setting(lease.owner, "endpoints", {}) or {}
        legacy = (endpoints.get("agents") or {}).get("toolApproval")
        return call_decision(mode, legacy, call.name, call.arguments) or {}

    def _receipt_access(self) -> dict:
        """Capture the effective disclosure ceiling, not a new grant.

        Some results (Job listings and Task metadata) contain more than one
        native resource. Preserve the full readable/writable ceiling rather
        than infer authorization from the provider's argument shape.
        """
        return self._effective_receipt_access(*self.current_grant())

    def _effective_receipt_access(
        self, scopes: set[str], expires: int | None
    ) -> dict:
        lease = self.checkpoint.lease
        conversation, agents = self.repository.tool_context_chain(
            lease, authorization=True
        )
        policy, _ = effective_machine_access(
            lease.owner,
            conversation,
            agent=agents[-1],
            ancestors=agents[:-1],
            scopes=scopes,
            expires=expires,
            repo=WorkspaceRepository(self.repository.database),
            registered=_registered(self.runtime),
        )
        policy = self.repository.cap_machine_access(policy)
        return {
            "scopes": sorted(scopes),
            "machines": {
                machine: level
                for machine, level in policy.items()
                if level != "none"
            },
            "task_machine": self.task_machine,
        }

    def _authorize_receipt(self, call: ModelToolCall, receipt: dict) -> None:
        expected = {"result", "access"}
        if call.name in _DELEGATION_TOOLS:
            expected.add("childScope")
        if not isinstance(receipt, dict) or set(receipt) != expected:
            raise PermissionError(
                "Native receipt lacks disclosure authorization evidence"
            )
        if call.name in _DELEGATION_TOOLS:
            if self.delegation is None:
                raise PermissionError("Foreground delegation is disabled")
            self.delegation.validate(call)
            if receipt["childScope"] is None:
                if receipt["result"].get("isError") is not True:
                    raise PermissionError("Subagent receipt lacks child scope")
            else:
                self.delegation.authorize_receipt(
                    self, call, receipt["childScope"]
                )
        access = receipt["access"]
        if not isinstance(access, dict) or set(access) != {
            "scopes",
            "machines",
            "task_machine",
        }:
            raise PermissionError(
                "Invalid native receipt authorization evidence"
            )
        old_scopes = access["scopes"]
        old_machines = access["machines"]
        if (
            not isinstance(old_scopes, list)
            or any(not isinstance(scope, str) for scope in old_scopes)
            or not isinstance(old_machines, dict)
            or any(
                not isinstance(machine, str) or level not in ("read", "write")
                for machine, level in old_machines.items()
            )
        ):
            raise PermissionError(
                "Invalid native receipt authorization evidence"
            )
        current = self._receipt_access()
        if (
            not set(old_scopes).issubset(current["scopes"])
            or any(
                current["machines"].get(machine)
                not in ({"read", "write"} if level == "read" else {"write"})
                for machine, level in old_machines.items()
            )
            or access["task_machine"] != current["task_machine"]
        ):
            raise PermissionError("Native receipt disclosure grant was reduced")

    async def dispatch(
        self,
        calls: list[ModelToolCall],
        state: AgentLoopState,
        *,
        authorize_effects: Callable[
            [list[ModelToolCall], AgentLoopState], Awaitable[None]
        ]
        | None = None,
    ) -> ToolBatchOutput:
        calls = copy.deepcopy(complete_tool_batch(calls))
        self._validate_question_batch(calls)
        claim_store = ApprovalClaimStore(self.repository.database)
        effective: dict[str, ModelToolCall] = {}
        blocked: dict[str, str] = {}
        reviewed: set[str] = set()
        child_scopes: dict[str, dict] = {}

        def check() -> None:
            self.current_grant()
            _, agents = self.repository.tool_context_chain(
                self.checkpoint.lease
            )
            enabled = _selected_tools(agents, self.allowed_tools)
            if self._question_enabled(agents[-1]):
                enabled = enabled | {ASK_USER_QUESTION_TOOL_NAME}
            enabled |= {
                tool["function"]["name"] for tool in self._handoffs()[1]
            }
            if self.delegation is not None and self.delegation.definitions():
                enabled |= {
                    tool["function"]["name"]
                    for tool in self.delegation.definitions()
                }
            if any(call.name not in enabled for call in calls):
                raise PermissionError("Native tool no longer selected")

        def rejected(reason: str) -> dict:
            return {
                "content": [{"type": "text", "text": reason}],
                "isError": True,
            }

        def refresh_decision(call: ModelToolCall) -> None:
            decision = self._decision(call)
            if decision.get("decision") == "deny":
                blocked[call.call_id] = "Blocked: " + decision.get(
                    "reason", "Rejected by policy"
                )
            elif (
                decision.get("decision") == "ask"
                and call.call_id not in reviewed
            ):
                raise PermissionError(
                    "Native approval policy changed before execution"
                )

        async def execute_native(call: ModelToolCall) -> dict:
            if call.call_id not in blocked:
                refresh_decision(effective[call.call_id])
            if call.call_id in blocked:
                return rejected(blocked[call.call_id])
            prepared = effective[call.call_id]
            if prepared.name == ASK_USER_QUESTION_TOOL_NAME:
                answer = claim_store.answered_question(self.checkpoint)
                if answer is None:
                    raise RuntimeError("Missing durable Ask User answer")
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                answer,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    ]
                }
            if is_transfer_tool(prepared.name):
                return {
                    "graphCommand": self._transfer(prepared, state.messages)
                }
            if prepared.name in _DELEGATION_TOOLS:
                if self.delegation is None:
                    raise PermissionError("Foreground delegation is disabled")
                result, scope = await self.delegation.invoke(
                    self, prepared, state.context, authorize_effects
                )
                child_scopes[call.call_id] = scope
                return result
            scopes, expires = self.current_grant()
            return await self.native.dispatch_model_call(
                self.repository,
                self.checkpoint.lease,
                prepared,
                allowed_tools=self.allowed_tools,
                scopes=scopes,
                expires=expires,
                task_machine=self.task_machine,
            )

        async def execute(call: ModelToolCall) -> dict:
            access = self._receipt_access()
            result = await execute_native(call)
            if access != self._receipt_access():
                raise PermissionError(
                    "Native receipt disclosure grant changed during execution"
                )
            receipt: dict = {"result": result, "access": access}
            if call.name in _DELEGATION_TOOLS:
                receipt["childScope"] = child_scopes.get(call.call_id)
            return receipt

        async def preflight_unsettled(unsettled: list[ModelToolCall]) -> None:
            child = isinstance(
                self.checkpoint.scope,
                (SubagentScope, SubagentGraphScope, DetachedExecutionScope),
            )
            child = child or self.checkpoint.lease.wakeup_task_id is not None
            claim = (
                None if child else claim_store.approved_batch(self.checkpoint)
            )
            entries = (
                {entry.call_id: entry for entry in claim.entries}
                if claim
                else {}
            )
            ask = []
            for call in unsettled:
                entry = entries.get(call.call_id)
                decision = self._decision(call)
                if decision.get("decision") == "deny":
                    blocked[call.call_id] = "Blocked: " + decision.get(
                        "reason", "Rejected by policy"
                    )
                    continue
                if entry is not None:
                    resolution = entry.decision
                    if resolution["type"] == "reject":
                        blocked[call.call_id] = "Blocked: " + resolution.get(
                            "reason", "Rejected by user"
                        )
                        continue
                    if resolution["type"] not in {"approve", "edit"}:
                        raise ValueError("Unsupported native approval decision")
                    reviewed.add(call.call_id)
                    effective[call.call_id] = entry.tool_call()
                    if (
                        call.name in _DELEGATION_TOOLS
                        and effective[call.call_id] != call
                    ):
                        raise PermissionError(
                            "Edited delegation requires a new child admission"
                        )
                elif decision.get("decision") == "ask":
                    if child:
                        blocked[call.call_id] = (
                            "Blocked: child tool requires human approval"
                        )
                    else:
                        ask.append(call)
                else:
                    effective[call.call_id] = call
            if ask and claim is not None:
                raise PermissionError(
                    "Native approval policy changed after review"
                )
            candidates = [*effective.values(), *ask]
            self._validate_question_batch(candidates)
            self._prepare(candidates)
            if authorize_effects is not None and candidates:
                await authorize_effects(copy.deepcopy(candidates), state)
            check()
            if ask:
                if self.activation is not None:
                    raise GraphHumanActionRequested(
                        GraphHumanRequest.from_checkpoint(self.checkpoint, ask)
                    )
                claim_store.pause(self.checkpoint, ask)
                raise AgentSuspended("Waiting for native tool approval")
            for call in effective.values():
                refresh_decision(call)
            if candidates and candidates[0].name == ASK_USER_QUESTION_TOOL_NAME:
                original = unsettled[0]
                if (
                    original.call_id not in blocked
                    and claim_store.answered_question(self.checkpoint) is None
                ):
                    if self.activation is not None:
                        raise GraphHumanActionRequested(
                            GraphHumanRequest.from_checkpoint(
                                self.checkpoint, [original], question=True
                            )
                        )
                    claim_store.pause_question(self.checkpoint, original)
                    raise AgentSuspended("Waiting for Ask User answer")

        outputs = await execute_checkpointed_batch(
            self.checkpoint,
            calls,
            execute,
            check_grant=check,
            preflight_unsettled=preflight_unsettled,
            authorize_receipt=self._authorize_receipt,
        )
        messages = []
        commands = []
        for call, output in zip(calls, outputs, strict=True):
            result = output["result"]
            if is_transfer_tool(call.name) and "graphCommand" in result:
                # StateGraph owns transfer acknowledgement projection and
                # parallel metadata. Do not emit a duplicate ordinary result.
                commands.append(result["graphCommand"])
            else:
                messages.append(
                    native_tool_history_message(
                        call.call_id,
                        result,
                        state.context.profile.provider,
                    )
                )
        return ToolBatchOutput(messages, graph_commands=commands)
