"""Control-owned, process-local generation driver tasks.

SQLite actor leases, not this registry, are the durable execution fence.
Only authenticated callers may submit a driver; restart never reconstructs
credentials or launches a task from persisted state.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class GenerationCapacityExceeded(RuntimeError):
    """The Control generation host has reached its bounded concurrency."""


class GenerationSupervisor:
    def __init__(self, *, global_limit: int = 32, owner_limit: int = 8):
        if not 0 < owner_limit <= global_limit:
            raise ValueError("Invalid generation host capacity")
        self._global_limit = global_limit
        self._owner_limit = owner_limit
        self._tasks: dict[tuple[str, str], asyncio.Task[bool]] = {}
        self._closed = False

    def active(self, owner: str, run_id: str) -> bool:
        task = self._tasks.get((owner, run_id))
        return task is not None and not task.done()

    def start(
        self, owner: str, run_id: str, execute: Callable[[], Awaitable[bool]]
    ) -> bool:
        """Coalesce duplicate submissions without constructing another driver."""
        if self._closed:
            raise RuntimeError("Generation supervisor is closed")
        if not owner or not run_id:
            raise ValueError("Generation identity is required")
        if self.active(owner, run_id):
            return False
        active = [key for key, task in self._tasks.items() if not task.done()]
        if (
            len(active) >= self._global_limit
            or sum(key[0] == owner for key in active) >= self._owner_limit
        ):
            raise GenerationCapacityExceeded("Generation host capacity reached")

        async def run() -> bool:
            return await execute()

        key = (owner, run_id)
        task = asyncio.create_task(run(), name="workgate-chat-generation")
        self._tasks[key] = task
        task.add_done_callback(lambda completed: self._settled(key, completed))
        return True

    def _settled(self, key: tuple[str, str], task: asyncio.Task[bool]) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                # Provider failures can include credentials in their request
                # URL. The durable generation already stores a safe error.
                logger.warning(
                    "Hosted Chat driver failed (%s)", type(error).__name__
                )

    async def aclose(self) -> None:
        """Cancel and join every driver before its Control services disappear."""
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
