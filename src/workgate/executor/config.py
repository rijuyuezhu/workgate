"""Resolved executor-plane configuration view."""

from dataclasses import dataclass
from pathlib import Path

from ..config.settings import Settings


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    """Executor-owned machine policy needed by new executor composition code."""

    workspace_root: Path
    allow_full_control: bool
    command_denylist: tuple[str, ...]
    path_denylist: tuple[str, ...]
    max_concurrent_commands: int
    max_tmux_sessions: int
    shell_executable: str
    tmux_bin: str
    rg_bin: str
    git_bin: str
    python_bin: str


def resolve_executor_config(settings: Settings) -> ExecutorConfig:
    """Snapshot executor-owned authority from the legacy monolithic settings."""
    return ExecutorConfig(
        workspace_root=settings.workspace_root.resolve(strict=False),
        allow_full_control=settings.allow_full_control,
        command_denylist=tuple(settings.command_denylist),
        path_denylist=tuple(settings.path_denylist),
        max_concurrent_commands=settings.max_concurrent_commands,
        max_tmux_sessions=settings.max_tmux_sessions,
        shell_executable=settings.shell_executable,
        tmux_bin=settings.tmux_bin,
        rg_bin=settings.rg_bin,
        git_bin=settings.git_bin,
        python_bin=settings.python_bin,
    )
