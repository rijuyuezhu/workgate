"""Build executor protocol hello state."""

import platform

from .. import __version__
from ..config.executor import ExecutorConfig
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


def build_executor_hello(
    config: ExecutorConfig,
    *,
    sessions: tuple[SessionInventorySummary, ...] = (),
    shells: tuple[ShellInventorySummary, ...] = (),
    jobs: tuple[JobInventorySummary, ...] = (),
) -> ExecutorHelloRequest:
    """Return one complete inventory of executor-owned resources."""
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
        default_workdir=str(config.default_workdir),
        sessions=sessions,
        shells=shells,
        jobs=jobs,
    )
