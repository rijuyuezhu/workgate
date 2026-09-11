"""Executor-owned shared-session orientation and cwd mutation helpers."""

import subprocess
from pathlib import Path

from ..schemas.result_models.session import GitSessionInfo, SessionStartOutput
from .config import ExecutorConfig
from .environment import collect_executor_session_environment
from .tool_session.store import AgentSession, ToolSessionStore

_INSTRUCTION_FILE_NAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING",
    "CONTRIBUTING.md",
)


def _git_output(
    config: ExecutorConfig, args: list[str], cwd: Path
) -> str | None:
    """Run a bounded git orientation command through executor configuration."""
    try:
        result = subprocess.run(
            [config.git_bin, *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_info(config: ExecutorConfig, cwd: Path) -> GitSessionInfo:
    """Return cheap git orientation for an executor-local workdir."""
    root = _git_output(config, ["rev-parse", "--show-toplevel"], cwd)
    if root is None:
        return GitSessionInfo(is_repo=False)
    branch = _git_output(config, ["branch", "--show-current"], cwd)
    if not branch:
        branch = _git_output(config, ["rev-parse", "--short", "HEAD"], cwd)
    dirty = None
    status = _git_output(config, ["status", "--porcelain"], cwd)
    if status is not None:
        dirty = bool(status)
    return GitSessionInfo(is_repo=True, root=root, branch=branch, dirty=dirty)


def _relative_display(path: Path, workspace_root: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(workspace_root))
    except ValueError:
        return str(resolved)


def _instruction_files(config: ExecutorConfig, workdir: Path) -> list[str]:
    """Discover nearby project instructions within executor workspace authority."""
    root = config.workspace_root
    found: list[str] = []
    current = workdir.resolve(strict=False)
    while True:
        try:
            current.relative_to(root)
        except ValueError:
            break
        for name in _INSTRUCTION_FILE_NAMES:
            candidate = current / name
            if candidate.is_file():
                found.append(_relative_display(candidate, root))
        if current == root or current.parent == current:
            break
        current = current.parent
    return found


def session_output(
    config: ExecutorConfig, session: AgentSession
) -> SessionStartOutput:
    """Return orientation reported only from the executor that owns the session."""
    workdir = Path(session.workdir)
    return SessionStartOutput(
        session_id=session.session_id,
        workdir=session.workdir,
        created_at=session.created_at,
        updated_at=session.updated_at,
        label=session.label,
        workspace_root=str(config.workspace_root),
        git=_git_info(config, workdir),
        instruction_files=_instruction_files(config, workdir),
        environment=collect_executor_session_environment(
            config,
            workdir=session.workdir,
        ),
        message="Use this session_id in subsequent workspace tool calls.",
    )


def change_session_cwd(
    config: ExecutorConfig,
    store: ToolSessionStore,
    session_id: str,
    workdir: str,
) -> SessionStartOutput:
    """Mutate one executor-owned session cwd and return refreshed orientation."""
    session = store.change_session_workdir(session_id, workdir)
    return session_output(config, session)
