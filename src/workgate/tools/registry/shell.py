"""Shell MCP tool registry."""

from ...ops.bash import bash_execute, run_python_code_execute
from ...ops.shell import (
    kill_persistent_shell_execute,
    list_persistent_shells_execute,
    read_persistent_shell_output_execute,
    resize_persistent_shell_execute,
    send_persistent_shell_input_execute,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.input_models.shell import (
    EnterArg,
    InputTextArg,
    LinesArg,
    PythonCodeArg,
    ShellAsyncArg,
    ShellColumnsArg,
    ShellCommandArg,
    ShellCwdArg,
    ShellEnvArg,
    ShellIdArg,
    ShellMaxOutputBytesArg,
    ShellNameArg,
    ShellPtyArg,
    ShellRowsArg,
    ShellTimeoutArg,
    ToolPurposeArg,
)
from ...schemas.result_models.shell import (
    KillPersistentShellOutput,
    ListPersistentShellsOutput,
    ReadPersistentShellOutput,
    ResizePersistentShellOutput,
    RunPythonCodeOutput,
    SendPersistentShellInputOutput,
    ShellExecutionOutput,
)
from ..contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry


class ShellToolRegistry(DeclarativeToolRegistry):
    """Register shell execution and persistent-shell companion tools."""

    name = "shell"
    """Registry group name used for tool-surface organization."""


shell_tool = ShellToolRegistry.get_tool_decorator()


def _bash_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Run terminal commands inside an explicit agent/workspace session for builds, tests, package managers, git inspection, one-off scripts, and other work that genuinely needs a shell. Pass the session_id returned by session_start. cwd defaults to the session workdir; any cwd override resolves inside that session workdir. Prefer specialized tools for file context and edits: use read/search/tree_view/glob_search/list_files for inspection, hashline_edit when editing copied read/search rows, edit_lines for structured snapshot-grounded precise edits, write_file only for new files or intentional whole-file replacements, and delete_file_or_dir only for intentional removals. Use bash when the task is a command, not when a structured tool can do the job more safely.

Default mode is bounded and returns captured stdout/stderr. Use run_python_code instead of bash when you want to execute an ad hoc Python snippet without manually writing a script file. Set async_=true for long-running non-interactive work; this returns a job_id owned by the same session_id and must be managed with the job companion. Set pty=true for executor-side interactive programs, REPLs, servers, or commands that need later input; this returns a shell_id for persistent-shell companion tools. Persistent-shell companion tools require both the owning session_id and the returned shell_id. Do not use shell_id with job. If both async_ and pty are true, PTY mode is used. Use env for multiline, quote-heavy, or caller-provided values instead of embedding them directly in the command. Current bounded command timeout default/cap: {settings.run_shell_default_timeout_s}/{settings.run_shell_max_timeout_s} seconds."""


@shell_tool(
    http_method="POST",
    http_path="/tools/bash",
    description=_bash_description,
    oauth_scopes=("shell:read", "shell:execute"),
)
async def bash(
    session_id: SessionIdArg,
    command: ShellCommandArg,
    cwd: ShellCwdArg = ".",
    timeout_s: ShellTimeoutArg = None,
    max_output_bytes: ShellMaxOutputBytesArg = None,
    env: ShellEnvArg = None,
    async_: ShellAsyncArg = False,
    pty: ShellPtyArg = False,
    name: ShellNameArg = None,
    purpose: ToolPurposeArg = None,
) -> ShellExecutionOutput:
    """Run a shell command via bounded, job, or PTY mode."""
    return await bash_execute(
        session_id,
        command,
        cwd,
        timeout_s,
        max_output_bytes,
        env,
        async_,
        pty,
        name,
    )


def _run_python_code_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Write Python code to a temporary file and execute it inside an explicit agent/workspace session. Pass the session_id returned by session_start. This is a convenience wrapper over bash that runs `python3 <temporary-script>` and supports the same cwd, timeout_s, max_output_bytes, env, async_, pty, and name controls. Use it for quick Python calculations, project-aware scripts, or structured file analysis where Python is clearer than a shell pipeline. Use bash instead when you already have a concrete terminal command; use read/search/hashline_edit/edit_lines/write_file when the task is file inspection or editing rather than script execution.

cwd defaults to the session workdir; any cwd override resolves inside that session workdir. Default mode is bounded and returns captured stdout/stderr under result. Set async_=true for a non-interactive background job owned by the same session_id and managed with job. Set pty=true for executor-side Python processes that need an interactive terminal, returning shell_id for persistent-shell companion tools. Current bounded command timeout default/cap: {settings.run_shell_default_timeout_s}/{settings.run_shell_max_timeout_s} seconds."""


@shell_tool(
    http_method="POST",
    http_path="/tools/run_python_code",
    description=_run_python_code_description,
    oauth_scopes=("shell:read", "shell:execute"),
)
async def run_python_code(
    session_id: SessionIdArg,
    code: PythonCodeArg,
    cwd: ShellCwdArg = ".",
    timeout_s: ShellTimeoutArg = None,
    max_output_bytes: ShellMaxOutputBytesArg = None,
    env: ShellEnvArg = None,
    async_: ShellAsyncArg = False,
    pty: ShellPtyArg = False,
    name: ShellNameArg = None,
    purpose: ToolPurposeArg = None,
) -> RunPythonCodeOutput:
    """Write Python code to a temporary file and execute it through shell modes."""
    return await run_python_code_execute(
        session_id,
        code,
        cwd,
        timeout_s,
        max_output_bytes,
        env,
        async_,
        pty,
        name,
    )


@shell_tool(
    http_method="POST",
    http_path="/tools/send_persistent_shell_input",
    oauth_scopes=("shell:read", "shell:execute"),
)
async def send_persistent_shell_input(
    session_id: SessionIdArg,
    shell_id: ShellIdArg,
    input_text: InputTextArg,
    enter: EnterArg = True,
) -> SendPersistentShellInputOutput:
    """Send input to an existing persistent shell created by bash(pty=true). Pass the owning session_id and shell_id returned by bash PTY mode or list_persistent_shells. Use this only for interactive or manually managed shells that need later input, such as REPLs, prompts, or development servers. Jobs started with bash(async_=true) are non-interactive background jobs; use the job companion for those instead. Set enter=false only when intentionally sending partial input without a newline."""
    return await send_persistent_shell_input_execute(
        shell_id, input_text, enter
    )


@shell_tool(
    http_method="POST",
    http_path="/tools/resize_persistent_shell",
    oauth_scopes=("shell:read", "shell:execute"),
)
async def resize_persistent_shell(
    session_id: SessionIdArg,
    shell_id: ShellIdArg,
    cols: ShellColumnsArg,
    rows: ShellRowsArg,
) -> ResizePersistentShellOutput:
    """Resize a persistent PTY shell created by bash(pty=true). Pass the owning session_id and shell_id returned by bash PTY mode or list_persistent_shells. Supply the visible terminal width and height so terminal UIs and interactive full-screen programs can track the client viewport."""
    return await resize_persistent_shell_execute(shell_id, cols, rows)


@shell_tool(
    http_method="POST",
    http_path="/tools/read_persistent_shell_output",
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def read_persistent_shell_output(
    session_id: SessionIdArg,
    shell_id: ShellIdArg,
    lines: LinesArg = 200,
) -> ReadPersistentShellOutput:
    """Read recent output from a persistent shell created by bash(pty=true). Pass the owning session_id and shell_id returned by bash PTY mode or list_persistent_shells. Use after send_persistent_shell_input to inspect an interactive or manually managed shell without blocking. For tracked non-interactive jobs from bash(async_=true), use job(poll=[...]) because it works from job_id and refreshes job status. lines defaults to 200 and controls how many recent terminal lines are returned; increase it only when needed for context."""
    return await read_persistent_shell_output_execute(shell_id, lines)


@shell_tool(
    http_method="POST",
    http_path="/tools/kill_persistent_shell",
    oauth_scopes=("shell:read", "shell:execute"),
)
async def kill_persistent_shell(
    session_id: SessionIdArg,
    shell_id: ShellIdArg,
) -> KillPersistentShellOutput:
    """Terminate a persistent shell created by bash(pty=true). Pass the owning session_id and shell_id returned by bash PTY mode or list_persistent_shells. Use for manually managed shells such as servers, watches, REPLs, or stuck interactive commands. For tracked non-interactive jobs from bash(async_=true), use job(cancel=[...]) so the job record is updated. This is destructive for that shell process but does not delete files."""
    return await kill_persistent_shell_execute(shell_id)


@shell_tool(
    http_method="GET",
    http_path="/tools/list_persistent_shells",
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def list_persistent_shells(
    session_id: SessionIdArg,
) -> ListPersistentShellsOutput:
    """List active persistent shells owned by the explicit session_id. Use this when you need the shell_id before reading, sending input, or killing a manually managed shell. Returned shell_id values are persistent-shell handles scoped by the owning session. Async bash jobs are not listed here; use job(session_id, list_jobs=true) to inspect bash(async_=true) background jobs."""
    return await list_persistent_shells_execute()
