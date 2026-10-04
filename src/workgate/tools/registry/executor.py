"""Executor fleet discovery and administration tool registry."""

from ...config.control import ControlConfig
from ...schemas.input_models.executor import (
    ExecutorActionArg,
    ExecutorIdArg,
    ExecutorNameArg,
)
from ...schemas.result_models.executor import ExecutorFleetOutput
from ..declarative import DeclarativeToolRegistry


class ExecutorToolRegistry(DeclarativeToolRegistry):
    """Register the compact executor fleet control surface."""

    name = "executor"


executor_tool = ExecutorToolRegistry.get_tool_decorator()


def _executor_tool_enabled(settings: ControlConfig) -> bool:
    return settings.mode in {"http", "mcp"}


@executor_tool(
    http_method="POST",
    http_path="/tools/executor",
    oauth_scopes=("executor:use",),
    timeout_cancellable=False,
    enabled=_executor_tool_enabled,
)
async def executor(
    action: ExecutorActionArg = "list",
    executor_id: ExecutorIdArg = None,
    name: ExecutorNameArg = None,
) -> ExecutorFleetOutput:
    """Discover executor choices or perform bounded fleet administration."""
    del action, executor_id, name
    raise RuntimeError("executor requires control routing")
