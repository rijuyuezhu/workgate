"""Role-owned Settings names used at process boundaries."""

CONTROL_EXCLUDED_SETTING_NAMES = frozenset(
    {
        "workspace_root",
        "allow_full_control",
        "run_shell_default_timeout_s",
        "run_shell_max_timeout_s",
        "max_output_bytes",
        "max_job_log_bytes",
        "max_jobs",
        "max_session_snapshots",
        "max_session_snapshot_bytes",
        "max_file_read_bytes",
        "max_view_image_bytes",
        "max_file_write_bytes",
        "max_grep_results",
        "max_directory_entries",
        "max_glob_results",
        "max_tree_entries",
        "max_tmp_files",
        "max_tmp_bytes",
        "max_transfer_archive_entries",
        "max_transfer_unpacked_bytes",
        "max_concurrent_commands",
        "max_tmux_sessions",
        "max_skills",
        "max_skill_related_files",
        "max_skill_scan_entries",
        "max_skill_path_bytes",
        "command_denylist",
        "path_denylist",
        "shell_executable",
        "tmux_bin",
        "rg_bin",
        "git_bin",
        "python_bin",
    }
)

EXECUTOR_SHARED_SETTING_NAMES = frozenset(
    {
        "state_dir",
        "ui_terminal_idle_timeout_s",
        "ui_terminal_max_connections",
        "agent_mcp_probe_timeout_s",
        "agent_mcp_call_timeout_s",
    }
)

EXECUTOR_SETTING_NAMES = (
    CONTROL_EXCLUDED_SETTING_NAMES | EXECUTOR_SHARED_SETTING_NAMES
)
