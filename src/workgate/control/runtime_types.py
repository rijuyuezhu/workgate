"""Structural control-runtime types for adapters that must not create import cycles."""

from typing import Protocol

from .executor_transport import ExecutorTransport
from .sessions import ControlSessionCoordinator


class ControlRuntimeLike(Protocol):
    """Minimum control runtime surface needed by executor-backed adapters."""

    executor_transport: ExecutorTransport
    session_coordinator: ControlSessionCoordinator
