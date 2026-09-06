"""Build executor protocol hello state for the final v1 resource namespace."""

from __future__ import annotations

import platform

from .. import __version__
from ..protocol.executor import ExecutorHelloRequest, ExecutorRuntimeSummary
from .config import ExecutorConfig


def build_executor_hello(config: ExecutorConfig) -> ExecutorHelloRequest:
    """Return one complete hello for resources already owned by executor v1.

    PR4 establishes the transport before PR6/PR8 migrate session, shell, and job
    identities into the final executor protocol namespace. Those legacy resources
    must not be projected into v1 under incompatible identifiers, so the current
    complete v1 resource set is empty rather than truncated.
    """
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(
            workgate_version=__version__,
            platform=platform.system()[:128] or None,
        ),
        capabilities=(),
        workspace_root=str(config.workspace_root),
        sessions=(),
        shells=(),
        jobs=(),
    )
