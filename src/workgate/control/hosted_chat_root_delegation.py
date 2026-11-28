"""Trusted root-only detached tool adapter; scheduling remains host-owned."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace

from ..hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    RuntimeSafetyError,
)
from ..hosted_chat.detached_subagents import (
    DetachedReservation,
    reserve_detached_child,
)
from ..hosted_chat.detached_task_controls import (
    CHECK_BACKGROUND_TASK,
    check_background_task_tools,
    check_detached_task,
    validate_background_task_call,
)
from ..hosted_chat.provider_common import ModelToolCall
from ..hosted_chat.subagent_scope import SubagentScope, _digest
from ..hosted_chat.subagents import (
    SUBAGENT_TOOL_NAME,
    SubagentInvocation,
    invoke_subagent,
    subagent_tools,
)
from .hosted_chat_native_tool_node import NativeLoopToolNode
from .hosted_chat_subagent_loop import ForegroundDelegation


class RootDelegation(ForegroundDelegation):
    def __init__(
        self,
        configs,
        resolve_context,
        *,
        on_reserved: Callable[[DetachedReservation, dict], Awaitable[None]],
        **kwargs,
    ):
        super().__init__(configs, resolve_context, **kwargs)
        self.on_reserved = on_reserved

    def definitions(self) -> list[dict]:
        tools = subagent_tools(
            self.configs, background=True, thread_continuation=True
        )
        if tools:
            tools[0]["function"]["parameters"]["properties"][
                "run_in_background"
            ]["description"] = (
                "Reserve a durable detached subagent and return its task ID. "
                "Use check_background_task to collect its result. Recovery requires a live authorized host."
            )
        return tools + check_background_task_tools()

    def validate(self, call: ModelToolCall) -> None:
        if call.name == CHECK_BACKGROUND_TASK:
            validate_background_task_call(call)
            return
        args = call.arguments
        if call.name != SUBAGENT_TOOL_NAME or not isinstance(args, dict):
            raise PermissionError("Unknown root delegation tool")
        if "run_in_background" in args and not isinstance(
            args["run_in_background"], bool
        ):
            raise ValueError("run_in_background must be boolean")
        if "subagent_thread_id" in args and (
            args.get("run_in_background") is not True
            or not isinstance(args["subagent_thread_id"], str)
            or not args["subagent_thread_id"].strip()
        ):
            raise ValueError(
                "Thread continuation requires background delegation and a thread ID"
            )
        super().validate(
            replace(
                call,
                arguments={
                    key: value
                    for key, value in args.items()
                    if key not in {"run_in_background", "subagent_thread_id"}
                },
            )
        )

    @staticmethod
    def _root(node: NativeLoopToolNode) -> None:
        if (
            node.checkpoint.scope is not None
            or node.checkpoint.lease.wakeup_task_id is not None
        ):
            raise PermissionError(
                "Detached tools require an ordinary root parent edge"
            )

    @staticmethod
    def _access(node: NativeLoopToolNode) -> dict:
        return {
            **node._receipt_access(),
            "allowed_tools": sorted(node.allowed_tools),
        }

    def _control(self, node, call, *, replay_only=False):
        scopes, expires = node.current_grant()
        return check_detached_task(
            node.checkpoint,
            call,
            scopes=scopes,
            expires=expires,
            disclosure_access=self._access(node),
            replay_only=replay_only,
        )

    def authorize_receipt(
        self, node: NativeLoopToolNode, call: ModelToolCall, record: dict
    ) -> None:
        self._root(node)
        self.validate(call)
        if call.name == CHECK_BACKGROUND_TASK:
            if record != {
                "kind": "control",
                "digest": _digest(self._control(node, call, replay_only=True)),
            }:
                raise PermissionError("Background task control receipt changed")
            return
        if call.arguments.get("run_in_background") is not True:
            super().authorize_receipt(node, call, record)
            return
        if (
            set(record) != {"kind", "scope", "task_id"}
            or record["kind"] != "detached"
        ):
            raise PermissionError("Invalid detached reservation receipt")
        scope = SubagentScope.from_record(record["scope"])
        super().authorize_receipt(node, call, record["scope"])
        access = self._access(node)
        lease = node.checkpoint.lease
        with lease.database.read_snapshot() as db:
            row = db.execute(
                "SELECT * FROM subagent_tasks WHERE id=? AND owner=? AND parent_run_id=?",
                (record["task_id"], lease.owner, lease.run_id),
            ).fetchone()
            if (
                row is None
                or not scope.detached
                or row["idempotency_key"] != scope.checkpoint_namespace
                or row["fingerprint"] != scope.invocation_hash
                or not row["child_registered"]
            ):
                raise PermissionError("Detached reservation no longer owned")
            options = json.loads(row["execution_options"])
            if (
                options["scope"] != record["scope"]
                or options["disclosure_access"] != access
            ):
                raise PermissionError("Detached reservation authority changed")

    async def invoke(
        self,
        node: NativeLoopToolNode,
        call: ModelToolCall,
        context: AgentContext,
        authorize_effects: Callable[
            [list[ModelToolCall], AgentLoopState], Awaitable[None]
        ]
        | None,
    ) -> tuple[dict, dict]:
        self._root(node)
        self.validate(call)
        if call.name == CHECK_BACKGROUND_TASK:
            result = self._control(node, call)
            return {
                "content": [{"type": "text", "text": json.dumps(result)}]
            }, {"kind": "control", "digest": _digest(result)}
        if call.arguments.get("run_in_background") is not True:
            return await super().invoke(node, call, context, authorize_effects)
        lease = node.checkpoint.lease
        record = None

        async def reserve(invocation: SubagentInvocation) -> str:
            nonlocal record
            access = self._access(node)
            reservation = reserve_detached_child(
                node.checkpoint, call, invocation, disclosure_access=access
            )
            with lease.database.read_snapshot() as db:
                row = db.execute(
                    "SELECT execution_options FROM subagent_tasks WHERE id=? AND owner=?",
                    (reservation.task_id, lease.owner),
                ).fetchone()
                record = {
                    "kind": "detached",
                    "scope": json.loads(row["execution_options"])["scope"],
                    "task_id": reservation.task_id,
                }
            self.authorize_receipt(node, call, record)
            if reservation.status == "running":
                await self.on_reserved(reservation, access)
            return json.dumps(
                {
                    "background_task_id": reservation.task_id,
                    "subagent_thread_id": reservation.thread_id,
                    "status": "reserved",
                }
            )

        async def unexpected(_invocation):
            raise RuntimeSafetyError(
                "Background delegation entered foreground execution"
            )

        result = await invoke_subagent(
            call,
            self.configs,
            execute_child=unexpected,
            execute_background=reserve,
            current_grant=node.current_grant,
            owner=lease.owner,
            conversation_id=node.checkpoint.active["configurable"]["thread_id"],
            parent_run_id=lease.run_id,
            database=lease.database,
            parent_agent_id=(context.agent or {}).get("id"),
            max_depth=self.max_depth,
        )
        if record is None:
            raise RuntimeSafetyError("Detached child failed before reservation")
        self.authorize_receipt(node, call, record)
        return {"content": [{"type": "text", "text": result}]}, record
