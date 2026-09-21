"""Build executor protocol hello state for the final v1 resource namespace."""

from __future__ import annotations

import platform

from .. import __version__
from ..protocol.executor import (
    EXECUTOR_CAPABILITY_BROWSER,
    EXECUTOR_CAPABILITY_SESSIONS,
    ExecutorHelloRequest,
    ExecutorRuntimeSummary,
    JobInventorySummary,
    SessionInventorySummary,
    ShellInventorySummary,
)
from .browser import browser_capability_available
from .config import ExecutorConfig


def build_executor_hello(
    config: ExecutorConfig,
    *,
    sessions: tuple[SessionInventorySummary, ...] = (),
    shells: tuple[ShellInventorySummary, ...] = (),
    jobs: tuple[JobInventorySummary, ...] = (),
) -> ExecutorHelloRequest:
    """Return one complete thin inventory for executor-owned v1 resources."""
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(
            workgate_version=__version__,
            platform=platform.system()[:128] or None,
        ),
        capabilities=(
            EXECUTOR_CAPABILITY_SESSIONS,
            *(
                (EXECUTOR_CAPABILITY_BROWSER,)
                if browser_capability_available()
                else ()
            ),
        ),
        workspace_root=str(config.workspace_root),
        sessions=sessions,
        shells=shells,
        jobs=jobs,
    )
