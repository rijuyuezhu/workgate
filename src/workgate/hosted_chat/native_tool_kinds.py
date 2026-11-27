"""Native tool classifications shared by approval and effect admission."""

SESSION_SHELL_EFFECT_TOOLS = frozenset(
    {"send_persistent_shell_input", "resize_persistent_shell"}
)
SESSION_FILE_READ_TOOLS = frozenset(
    {"read", "list_files", "tree_view", "glob_search", "search"}
)
SESSION_SHELL_READ_TOOLS = frozenset(
    {"read_persistent_shell_output", "list_persistent_shells"}
)

SESSION_FILE_EFFECT_TOOLS = frozenset(
    {
        "write_file",
        "edit_lines",
        "hashline_edit",
        "delete_file_or_dir",
        "apply_patch",
    }
)


SESSION_COMMAND_EFFECT_TOOLS = frozenset({"bash", "run_python_code"})
SESSION_METADATA_EFFECT_TOOLS = frozenset({"session_change_workdir"})
TASK_METADATA_EFFECT_TOOLS = frozenset({"task", "task_plan", "write_todos"})
