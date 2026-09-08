"""Declarative remote-worker tool proxy specifications."""

from dataclasses import dataclass

REMOTE_WORKER_ORIGIN_ARG = "_workgate_origin"
REMOTE_WORKER_ORIGIN_MODEL = "model"
REMOTE_WORKER_ORIGIN_HUMAN_UI = "human_ui"


@dataclass(frozen=True)
class RemoteWorkerToolSpec:
    """Describe one remote worker tool and its optional public REST proxy."""

    public_name: str
    """Public proxy tool name exposed by the control server."""
    worker_tool: str
    """Underlying tool name executed on the remote worker."""
    http_path: str | None
    """Public REST path for the proxy, or None for worker-internal tools."""
    timeout_arg: str | None = None
    """Optional argument name that should receive the proxy timeout."""
    default_timeout: int | None = None
    """Default timeout passed through when the caller omits one."""

    @property
    def expose_http(self) -> bool:
        """Return whether this worker tool has a public REST proxy."""
        return bool(self.public_name and self.http_path)


REMOTE_WORKER_TOOL_SPECS: tuple[RemoteWorkerToolSpec, ...] = (
    RemoteWorkerToolSpec("", "open_terminal_bridge", None),
    RemoteWorkerToolSpec("", "read_terminal_bridge", None),
    RemoteWorkerToolSpec("", "write_terminal_bridge", None),
    RemoteWorkerToolSpec("", "resize_terminal_bridge", None),
    RemoteWorkerToolSpec("", "close_terminal_bridge", None),
    RemoteWorkerToolSpec("", "start_persistent_shell", None),
    RemoteWorkerToolSpec("", "send_persistent_shell_input", None),
    RemoteWorkerToolSpec("", "resize_persistent_shell", None),
    RemoteWorkerToolSpec("", "read_persistent_shell_output", None),
    RemoteWorkerToolSpec("", "kill_persistent_shell", None),
    RemoteWorkerToolSpec("", "list_persistent_shells", None),
    RemoteWorkerToolSpec(
        "", "run_python_code", None, timeout_arg="timeout_s", default_timeout=60
    ),
    RemoteWorkerToolSpec("", "session_start", None),
    RemoteWorkerToolSpec("", "session_change_cwd", None),
    RemoteWorkerToolSpec("", "session_end", None),
    RemoteWorkerToolSpec("", "dashboard_snapshot", None),
    RemoteWorkerToolSpec("", "query_audit", None),
    RemoteWorkerToolSpec("", "get_audit_entry", None),
    RemoteWorkerToolSpec("", "ui_session_snapshot", None),
    RemoteWorkerToolSpec("", "read_todos", None),
    RemoteWorkerToolSpec("", "write_todos", None),
    RemoteWorkerToolSpec("", "list_agent_skills", None),
    RemoteWorkerToolSpec("", "activate_agent_skill", None),
    RemoteWorkerToolSpec("", "read_agent_skill_file", None),
    RemoteWorkerToolSpec("", "list_files", None),
    RemoteWorkerToolSpec("", "tree_view", None),
    RemoteWorkerToolSpec("", "glob_search", None),
    RemoteWorkerToolSpec("", "write_file", None),
    RemoteWorkerToolSpec("", "delete_file_or_dir", None),
    RemoteWorkerToolSpec("", "secret_scan", None),
    RemoteWorkerToolSpec("", "read", None),
    RemoteWorkerToolSpec("", "search", None),
    RemoteWorkerToolSpec("", "view_image", None),
    RemoteWorkerToolSpec("", "workspace_search", None),
    RemoteWorkerToolSpec("", "fetch", None),
    RemoteWorkerToolSpec("", "edit_lines", None),
    RemoteWorkerToolSpec("", "hashline_edit", None),
    RemoteWorkerToolSpec("", "apply_patch", None),
    RemoteWorkerToolSpec("", "bash", None),
    RemoteWorkerToolSpec("", "job", None),
    RemoteWorkerToolSpec("", "transfer_stat", None),
    RemoteWorkerToolSpec("", "transfer_copy_file", None),
    RemoteWorkerToolSpec("", "transfer_read_chunk", None),
    RemoteWorkerToolSpec("", "transfer_begin_write", None),
    RemoteWorkerToolSpec("", "transfer_write_chunk", None),
    RemoteWorkerToolSpec("", "transfer_finish_write", None),
    RemoteWorkerToolSpec("", "transfer_abort_write", None),
    RemoteWorkerToolSpec("", "transfer_alloc_temp_path", None),
    RemoteWorkerToolSpec("", "transfer_pack_dir", None),
    RemoteWorkerToolSpec("", "transfer_unpack_archive", None),
    RemoteWorkerToolSpec("", "transfer_delete_temp_path", None),
    RemoteWorkerToolSpec("", "transfer_http_upload", None),
    RemoteWorkerToolSpec("", "transfer_http_download", None),
    RemoteWorkerToolSpec("", "transfer_http_abort_download", None),
)

REMOTE_WORKER_TOOL_NAMES = frozenset(
    spec.worker_tool for spec in REMOTE_WORKER_TOOL_SPECS
)
