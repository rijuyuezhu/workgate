"""Conversation-scoped scheduling under a live authenticated caller."""

import asyncio
import contextlib
import copy
import json
import time
from collections.abc import Awaitable, Callable, Collection

from ..hosted_chat.agent_runtime import AgentContext, AgentLoopState
from ..hosted_chat.database import SQLiteDatabase
from ..hosted_chat.detached_subagents import DetachedReservation
from ..hosted_chat.detached_task_lease import reconcile_reservations
from ..hosted_chat.generations import Generation
from ..hosted_chat.provider_common import ModelToolCall
from ..hosted_chat.providers import stream_model, stream_model_with_tools
from .hosted_chat_detached_worker import run_detached_subagent
from .hosted_chat_native_creation import _grant
from .hosted_chat_wakeup_worker import run_authenticated_wakeup
from .runtime import ControlRuntime

_POLL_SECONDS = 1
_MAX_WORKERS = 4


class AuthenticatedChildHost:
    """Schedule existing leased workers, never use persisted grants as credentials.

    The authenticated caller owns serve() and cancels it at shutdown. A fresh
    caller may recover matching reservations; no process-start sweep executes
    tasks with stored authority alone.
    """

    def __init__(
        self,
        runtime: ControlRuntime,
        database: SQLiteDatabase,
        owner: str,
        conversation: str,
        *,
        scopes: set[str],
        expires: int | None,
        allowed_tools: Collection[str],
        check_grant: Callable[[], None],
        current_access: Callable[[], dict],
        resolve_child: Callable[
            [Generation, dict | None], Awaitable[AgentContext]
        ],
        resolve_wakeup: Callable[[Generation], Awaitable[AgentContext]],
        authorize_batch: Callable[
            [list[ModelToolCall], AgentLoopState], Awaitable[None]
        ],
        stream_tools=stream_model_with_tools,
        stream_text=stream_model,
    ):
        if database.root != runtime.config.state_dir / "hosted-chat":
            raise PermissionError(
                "Child host belongs to another Control runtime"
            )
        self.runtime = runtime
        self.database = database
        self.owner = owner
        self.conversation = conversation
        self.scopes = frozenset(scopes)
        self.expires = expires
        self.allowed_tools = frozenset(allowed_tools)
        self.check_grant = check_grant
        self.current_access = current_access
        self.resolve_child = resolve_child
        self.resolve_wakeup = resolve_wakeup
        self.authorize_batch = authorize_batch
        self.stream_tools = stream_tools
        self.stream_text = stream_text
        self._wake = asyncio.Event()
        self._serving = False
        self._closed = False

    def _access(self) -> dict:
        self.check_grant()
        scopes, deadline = _grant(
            self.runtime, self.owner, set(self.scopes), self.expires
        )
        if not self.scopes.issubset(scopes) or (
            deadline is not None and deadline <= time.time()
        ):
            raise PermissionError(
                "Child host authentication grant expired or reduced"
            )
        access = copy.deepcopy(self.current_access())
        if access.get("scopes") != sorted(scopes) or access.get(
            "allowed_tools"
        ) != sorted(self.allowed_tools):
            raise PermissionError("Child host disclosure authority changed")
        return access

    async def on_reserved(
        self, reservation: DetachedReservation, access: dict
    ) -> None:
        """Wake the authenticated host after the reservation commits."""
        if self._closed or access != self._access():
            raise PermissionError("Child host no longer authorizes reservation")
        with self.database.read_snapshot() as db:
            row = db.execute(
                "SELECT execution_options FROM subagent_tasks WHERE id=? AND owner=? AND parent_conversation_id=? AND thread_id=? AND child_conversation_id=?",
                (
                    reservation.task_id,
                    self.owner,
                    self.conversation,
                    reservation.thread_id,
                    reservation.child_conversation_id,
                ),
            ).fetchone()
        if row is None:
            raise PermissionError("Reservation belongs to another child host")
        if (
            json.loads(row["execution_options"]).get("disclosure_access")
            != access
        ):
            raise PermissionError("Reservation disclosure authority changed")
        self._wake.set()

    def _candidates(self, access: dict) -> list[tuple[str, str]]:
        now = int(time.time() * 1000)
        with self.database.read_snapshot() as db:
            rows = db.execute(
                """SELECT t.id, 'child' AS kind, t.execution_options, t.created_at
                FROM subagent_tasks t
                WHERE t.owner=? AND t.parent_conversation_id=? AND t.status='running'
                AND (t.lease_until IS NULL OR t.lease_until<=?)
                UNION ALL
                SELECT t.id, 'wakeup', t.execution_options, w.created_at
                FROM subagent_wakeups w JOIN subagent_tasks t ON t.id=w.task_id
                WHERE w.owner=? AND w.parent_conversation_id=?
                AND t.owner=w.owner AND t.parent_conversation_id=w.parent_conversation_id
                AND w.status IN ('pending','dispatched') AND t.status!='running'
                AND (w.lease_until IS NULL OR w.lease_until<=?)
                AND (w.next_attempt_at IS NULL OR w.next_attempt_at<=?)
                ORDER BY created_at,id,kind""",
                (
                    self.owner,
                    self.conversation,
                    now,
                    self.owner,
                    self.conversation,
                    now,
                    now,
                ),
            ).fetchall()
        return [
            (row["kind"], row["id"])
            for row in rows
            if json.loads(row["execution_options"]).get("disclosure_access")
            == access
        ]

    async def _execute(self, kind: str, task_id: str, access: dict) -> None:
        def check() -> None:
            if self._access() != access:
                raise PermissionError("Child host disclosure authority changed")

        kwargs = {
            "scopes": set(self.scopes),
            "expires": self.expires,
            "disclosure_access": access,
            "allowed_tools": self.allowed_tools,
            "check_grant": check,
            "authorize_batch": self.authorize_batch,
            "stream_tools": self.stream_tools,
            "stream_text": self.stream_text,
        }
        if kind == "child":
            await run_detached_subagent(
                self.runtime,
                self.database,
                self.owner,
                self.conversation,
                task_id,
                resolve_context=self.resolve_child,
                **kwargs,
            )
        else:
            await run_authenticated_wakeup(
                self.runtime,
                self.database,
                self.owner,
                task_id,
                resolve_context=self.resolve_wakeup,
                **kwargs,
            )

    async def serve(self) -> None:
        """Run until the caller cancels or its live authority is withdrawn."""
        if self._serving or self._closed:
            raise RuntimeError("Child host can only be served once")
        self._serving = True
        active: dict[tuple[str, str], asyncio.Task[None]] = {}
        denied: set[tuple[str, str]] = set()
        try:
            while True:
                access = self._access()
                reconcile_reservations(
                    self.database, self.owner, self.conversation
                )
                for key, task in tuple(active.items()):
                    if task.done():
                        del active[key]
                        try:
                            task.result()
                        except PermissionError:
                            # A fresh grant for the same owner may permit recovery;
                            # do not repeatedly claim it under this host.
                            denied.add(key)
                for key in self._candidates(access):
                    if len(active) >= _MAX_WORKERS:
                        break
                    if key not in active and key not in denied:
                        active[key] = asyncio.create_task(
                            self._execute(*key, access)
                        )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=_POLL_SECONDS
                    )
                self._wake.clear()
        except PermissionError:
            # Revocation stops the host, rather than converting persisted
            # reservations into an unattended execution grant.
            return
        finally:
            self._closed = True
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
