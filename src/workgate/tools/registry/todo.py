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
    """Read the legacy Todo compatibility view of one explicit Workgate session's canonical plan. The returned items and revision come from the same control-owned task document used by read_session_task/update_session_plan; this is not a second checklist. The view remains readable after session_end as retained history. For new plan-aware clients prefer read_session_task and update_session_plan."""
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
    """Replace canonical plan steps through the legacy Todo compatibility surface for one explicit active Workgate session. Provide the full desired list; omitted steps are removed, while plan-only metadata on retained step IDs is preserved. The write shares the canonical task revision, so use expected_revision from read_todos when stale replacement must be rejected. Prefer update_session_plan for new clients that need blocked/skipped states or plan notes."""
    del session_id, todos, expected_revision
    raise RuntimeError("write_todos requires control routing")
