"""Build executor protocol hello state for the final v1 resource namespace."""

from __future__ import annotations

import platform

from .. import __version__
from ..protocol.executor import (
    EXECUTOR_CAPABILITY_SESSIONS,
    ExecutorHelloRequest,
    ExecutorRuntimeSummary,
    SessionInventorySummary,
)
from .config import ExecutorConfig


def build_executor_hello(
    config: ExecutorConfig,
    *,
    sessions: tuple[SessionInventorySummary, ...] = (),
) -> ExecutorHelloRequest:
    """Return one complete hello for resources already owned by executor v1.

    PR6 owns final shared session identities. Shell/job identity migration remains
    later work, so those resource sets stay empty rather than projecting legacy
    identifiers into the v1 namespace.
    """
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(
            workgate_version=__version__,
            platform=platform.system()[:128] or None,
        ),
        capabilities=(EXECUTOR_CAPABILITY_SESSIONS,),
        workspace_root=str(config.workspace_root),
        sessions=sessions,
        shells=(),
        jobs=(),
    )
