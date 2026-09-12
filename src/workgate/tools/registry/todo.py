"""Todo MCP tool registry."""

from ...schemas.input_models.session import SessionIdArg
from ...schemas.input_models.todo import ExpectedTodoRevisionArg, TodosArg
from ...schemas.result_models.todo import ReadTodosOutput, WriteTodosOutput
from ..declarative import DeclarativeToolRegistry


class TodoToolRegistry(DeclarativeToolRegistry):
    """Register todo-list tools."""

    name = "todo"
    """Registry group name used for tool-surface organization."""


todo_tool = TodoToolRegistry.get_tool_decorator()


@todo_tool(
    http_method="GET",
    http_path="/tools/todo",
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def read_todos(session_id: SessionIdArg) -> ReadTodosOutput:
    """Read the structured todo list owned by one explicit executor-backed agent/workspace session. Pass the shared session_id returned by session_start. Use this when resuming or checking multi-step work in the current session before deciding what to do next. Todos are session-scoped: items from one session are not shared with another session, and shell_id/job_id values are not valid here. For changing the list, use write_todos with the complete replacement list."""
    del session_id
    raise RuntimeError("read_todos requires control routing")


@todo_tool(
    http_method="POST",
    http_path="/tools/todo",
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def write_todos(
    session_id: SessionIdArg,
    todos: TodosArg,
    expected_revision: ExpectedTodoRevisionArg = None,
) -> WriteTodosOutput:
    """Replace the structured todo list owned by one explicit agent/workspace session. Pass the session_id returned by session_start and provide the full desired todo list, not a partial patch; omitted existing items are removed. Use expected_revision from read_todos when a stale replacement must be rejected. Keep todo content concise and actionable."""
    del session_id, todos, expected_revision
    raise RuntimeError("write_todos requires control routing")
