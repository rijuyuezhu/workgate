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
    ui_terminal_idle_timeout_s: int
    ui_terminal_max_connections: int
    run_shell_default_timeout_s: int
    run_shell_max_timeout_s: int
    max_output_bytes: int
    max_job_log_bytes: int
    max_jobs: int
    max_file_read_bytes: int
    max_session_snapshots: int
    max_session_snapshot_bytes: int
    max_transfer_archive_entries: int
    max_transfer_unpacked_bytes: int
    max_tmp_files: int
    max_tmp_bytes: int
    max_file_write_bytes: int
    max_view_image_bytes: int
    max_grep_results: int
    max_glob_results: int
    max_tree_entries: int
    max_directory_entries: int
    agent_config_dir: Path
    agent_auth_dir: Path
    agent_mcp_probe_timeout_s: int
    agent_mcp_call_timeout_s: int
    temp_dir: Path
    max_skills: int
    max_skill_related_files: int
    max_skill_scan_entries: int
    max_skill_path_bytes: int
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
        ui_terminal_idle_timeout_s=settings.ui_terminal_idle_timeout_s,
        ui_terminal_max_connections=settings.ui_terminal_max_connections,
        run_shell_default_timeout_s=settings.run_shell_default_timeout_s,
        run_shell_max_timeout_s=settings.run_shell_max_timeout_s,
        max_output_bytes=settings.max_output_bytes,
        max_job_log_bytes=settings.max_job_log_bytes,
        max_jobs=settings.max_jobs,
        max_file_read_bytes=settings.max_file_read_bytes,
        max_session_snapshots=settings.max_session_snapshots,
        max_session_snapshot_bytes=settings.max_session_snapshot_bytes,
        max_transfer_archive_entries=settings.max_transfer_archive_entries,
        max_transfer_unpacked_bytes=settings.max_transfer_unpacked_bytes,
        max_tmp_files=settings.max_tmp_files,
        max_tmp_bytes=settings.max_tmp_bytes,
        max_file_write_bytes=settings.max_file_write_bytes,
        max_view_image_bytes=settings.max_view_image_bytes,
        max_grep_results=settings.max_grep_results,
        max_glob_results=settings.max_glob_results,
        max_tree_entries=settings.max_tree_entries,
        max_directory_entries=settings.max_directory_entries,
        agent_config_dir=settings.agent_config_dir.resolve(strict=False),
        agent_auth_dir=settings.agent_auth_dir.resolve(strict=False),
        agent_mcp_probe_timeout_s=settings.agent_mcp_probe_timeout_s,
        agent_mcp_call_timeout_s=settings.agent_mcp_call_timeout_s,
        temp_dir=(settings.runtime_dir / "executor" / "tmp").resolve(
            strict=False
        ),
        max_skills=settings.max_skills,
        max_skill_related_files=settings.max_skill_related_files,
        max_skill_scan_entries=settings.max_skill_scan_entries,
        max_skill_path_bytes=settings.max_skill_path_bytes,
        shell_executable=settings.shell_executable,
        tmux_bin=settings.tmux_bin,
        rg_bin=settings.rg_bin,
        git_bin=settings.git_bin,
        python_bin=settings.python_bin,
    )
