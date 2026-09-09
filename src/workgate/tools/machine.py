"""Process-neutral classification for tools that require executor machine authority."""

# Keep this contract dependency-light so control can classify executor-routed tools
# without importing executor implementations or legacy remote-worker packages.
MACHINE_TOOL_NAMES = frozenset(
    {
        "activate_agent_skill",
        "apply_patch",
        "bash",
        "close_terminal_bridge",
        "dashboard_snapshot",
        "delete_file_or_dir",
        "edit_lines",
        "fetch",
        "glob_search",
        "hashline_edit",
        "kill_persistent_shell",
        "list_agent_skills",
        "list_files",
        "list_persistent_shells",
        "open_terminal_bridge",
        "read",
        "read_agent_skill_file",
        "read_persistent_shell_output",
        "read_terminal_bridge",
        "resize_persistent_shell",
        "resize_terminal_bridge",
        "run_python_code",
        "search",
        "secret_scan",
        "send_persistent_shell_input",
        "start_persistent_shell",
        "transfer_abort_write",
        "transfer_alloc_temp_path",
        "transfer_begin_write",
        "transfer_copy_file",
        "transfer_delete_temp_path",
        "transfer_finish_write",
        "transfer_pack_dir",
        "transfer_read_chunk",
        "transfer_stat",
        "transfer_unpack_archive",
        "transfer_write_chunk",
        "tree_view",
        "view_image",
        "workspace_search",
        "write_file",
        "write_terminal_bridge",
    }
)

# Internal executor-only operations used by mixed-placement public integrations.
# They are not themselves public machine tools, so control may keep HTTP/SSE
# Agent Bridge calls local while routing only session-bound stdio work here.
EXECUTOR_AGENT_MCP_OPERATION_NAMES = frozenset(
    {
        "agent_mcp.list_servers",
        "agent_mcp.list_tools",
        "agent_mcp.call_tool",
    }
)
