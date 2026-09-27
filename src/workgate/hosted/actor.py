"""Stateful hosted-control actor core without provider-specific HTTP glue."""

from contextlib import nullcontext
from dataclasses import dataclass, field

from ..config.control import ControlConfig, resolve_control_config
from ..config.settings import Settings
from ..control.executor_transport import ExecutorTransport
from ..control.pairing import ExecutorPairingService
from ..control.sessions import ControlSessionCoordinator
from ..control.state import ControlState
from ..control.streams import ControlStreamHub
from ..persistence import StateStore


@dataclass
class HostedControlActorCore:
    """One personal actor with durable facts and process-local live state."""

    config: ControlConfig
    state_store: StateStore
    control_state: ControlState
    executor_transport: ExecutorTransport
    executor_pairing: ExecutorPairingService
    session_coordinator: ControlSessionCoordinator
    stream_hub: ControlStreamHub
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def start(self) -> None:
        """Load durable facts and admit executor transport activity."""
        if self._closed:
            raise RuntimeError(
                "HostedControlActorCore cannot restart after close"
            )
        if self._started:
            return
        self.control_state.start()
        self.executor_transport.start()
        self._started = True

    async def aclose(self) -> None:
        """Drop all live state without deleting durable trust/session facts."""
        if self._closed:
            return
        self._closed = True
        self._started = False
        try:
            await self.stream_hub.aclose()
        finally:
            try:
                await self.executor_pairing.aclose()
            finally:
                try:
                    await self.session_coordinator.aclose()
                finally:
                    try:
                        await self.executor_transport.aclose()
                    finally:
                        self.control_state.close()


def build_hosted_control_actor_core(
    settings: Settings,
    *,
    state_store: StateStore,
) -> HostedControlActorCore:
    """Compose the provider-neutral subset suitable for one stateful actor."""
    config = resolve_control_config(settings)
    # Python Workers execute in Pyodide where ``threading`` is importable but
    # not functional. Durable Object request code is single-threaded, and these
    # ControlState operations contain no await points, so a reusable no-op
    # context lock preserves the synchronous critical-section boundary without
    # constructing ``threading.RLock``.
    control_state = ControlState(state_store, lock=nullcontext())
    executor_transport = ExecutorTransport(
        control_state,
        max_pending_commands=config.executor_max_pending_commands,
    )
    executor_pairing = ExecutorPairingService(
        control_state,
        executor_transport,
        verification_uri=config.resolved_base_url.rstrip("/") + "/pair",
        max_pending_attempts=config.executor_pairing_max_pending,
        ttl_s=config.executor_pairing_ttl_s,
    )
    session_coordinator = ControlSessionCoordinator(
        control_state,
        executor_transport,
        max_agent_sessions=config.max_agent_sessions,
        agent_session_retention_s=config.agent_session_retention_s,
    )
    stream_hub = ControlStreamHub(
        max_streams=config.ui_terminal_max_connections,
        idle_timeout_s=config.ui_terminal_idle_timeout_s,
    )

    async def authenticated_proof(executor_id: str, credential: str) -> None:
        await executor_pairing.complete_authenticated_proof(
            executor_id, credential
        )

    async def authenticated_hello(executor_id: str, credential: str) -> None:
        _ = credential
        await session_coordinator.reconcile_hello(executor_id)

    executor_transport.set_authenticated_proof_callback(authenticated_proof)
    executor_transport.set_authenticated_hello_callback(authenticated_hello)
    executor_transport.set_executor_invalidated_callback(
        stream_hub.close_executor
    )
    return HostedControlActorCore(
        config=config,
        state_store=state_store,
        control_state=control_state,
        executor_transport=executor_transport,
        executor_pairing=executor_pairing,
        session_coordinator=session_coordinator,
        stream_hub=stream_hub,
    )
