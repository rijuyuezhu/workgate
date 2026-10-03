"""Todo compatibility MCP tool registry."""

from ...schemas.input_models.task import TaskIdArg
from ...schemas.input_models.todo import TodosArg
from ...schemas.result_models.todo import ReadTodosOutput, WriteTodosOutput
from ..declarative import DeclarativeToolRegistry


class TodoToolRegistry(DeclarativeToolRegistry):
    """Register the Todo projection of semantic task plans."""

    name = "todo"


todo_tool = TodoToolRegistry.get_tool_decorator()


@todo_tool(
    http_method="GET",
    http_path="/tools/todo",
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def read_todos(task_id: TaskIdArg) -> ReadTodosOutput:
    """Read the Todo projection of one task's canonical plan."""
    del task_id
    raise RuntimeError("read_todos requires control routing")


@todo_tool(
    http_method="POST",
    http_path="/tools/todo",
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def write_todos(
    task_id: TaskIdArg,
    todos: TodosArg,
) -> WriteTodosOutput:
    """Replace one task's canonical plan through the Todo compatibility surface."""
    del task_id, todos
    raise RuntimeError("write_todos requires control routing")
