"""Shell MCP tool registry."""

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
    del context
    return """Run a command using session_id on the session's bound executor for builds, tests, git, or scripts. Bounded mode returns output; async_=true starts a tracked job_id for job, while pty=true starts an interactive shell_id for persistent-shell tools. PTY takes precedence if both are set. The executor enforces its timeout limits."""


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
    del (
        session_id,
        command,
        cwd,
        timeout_s,
        max_output_bytes,
        env,
        async_,
        pty,
        name,
        purpose,
    )
    raise RuntimeError("bash requires control routing")


def _run_python_code_description(context: McpToolContext) -> str:
    del context
    return """Run an ad hoc Python script on the session's executor without managing a temporary file. Supports bounded, async job, and interactive PTY modes like bash; use bash for existing commands."""


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
    del (
        session_id,
        code,
        cwd,
        timeout_s,
        max_output_bytes,
        env,
        async_,
        pty,
        name,
        purpose,
    )
    raise RuntimeError("run_python_code requires control routing")


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
    """Send input to the session-owned PTY shell_id from bash(pty=true); enter=false omits the newline. Async jobs use job instead."""
    del session_id, shell_id, input_text, enter
    raise RuntimeError("send_persistent_shell_input requires control routing")


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
    """Resize an existing session-owned PTY shell_id to the specified terminal rows and columns."""
    del session_id, shell_id, cols, rows
    raise RuntimeError("resize_persistent_shell requires control routing")


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
    """Read recent output from a session-owned PTY shell_id without blocking. For async job_id output, use job(poll=[...]) instead."""
    del session_id, shell_id, lines
    raise RuntimeError("read_persistent_shell_output requires control routing")


@shell_tool(
    http_method="POST",
    http_path="/tools/kill_persistent_shell",
    oauth_scopes=("shell:read", "shell:execute"),
)
async def kill_persistent_shell(
    session_id: SessionIdArg,
    shell_id: ShellIdArg,
) -> KillPersistentShellOutput:
    """Terminate a session-owned PTY shell_id (destructive to that process, not its files). For async jobs use job(cancel=[...])."""
    del session_id, shell_id
    raise RuntimeError("kill_persistent_shell requires control routing")


@shell_tool(
    http_method="GET",
    http_path="/tools/list_persistent_shells",
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def list_persistent_shells(
    session_id: SessionIdArg,
) -> ListPersistentShellsOutput:
    """List session-owned PTY shell_id handles; async jobs are listed through job instead."""
    del session_id
    raise RuntimeError("list_persistent_shells requires control routing")
