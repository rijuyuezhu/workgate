"""MCP response assertions and shell commands for tests."""

import base64
import shlex
import sys
from typing import Any, cast

from mcp.types import CallToolResult


def _mcp_content(response: Any, index: int = 0) -> Any:
    if isinstance(response, CallToolResult):
        return response.content[index]
    first = response[0]
    if isinstance(first, list):
        return first[index]
    return response[index]


def mcp_text(response: Any, index: int = 0) -> str:
    """Return text from a MCPServer call_tool response in tests."""
    return str(_mcp_content(response, index).text)


def nested_mcp_text(
    response: Any, index: int = 0, nested_index: int = 0
) -> str:
    """Return text from nested MCPServer responses produced by fetch tests."""
    content = _mcp_content(response, index)
    if isinstance(content, list):
        return str(content[nested_index].text)
    return str(content.text)


def mcp_structured(response: Any) -> dict[str, Any]:
    """Return structured content from a MCPServer structured-output response."""
    if isinstance(response, CallToolResult):
        assert isinstance(response.structured_content, dict)
        return cast(dict[str, Any], response.structured_content)
    assert isinstance(response, tuple)
    assert isinstance(response[1], dict)
    return response[1]


def python_shell_command(source: str) -> str:
    """Return one shell-safe Python command for POSIX shells."""
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    wrapper = f"import base64;exec(base64.b64decode('{encoded}'))"
    argv = [sys.executable, "-c", wrapper]
    return shlex.join(argv)
