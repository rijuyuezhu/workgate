"""Declarative tool registration."""

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, ClassVar, Literal, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from ..config.control import ControlConfig, get_control_config
from ..errors import (
    PublicToolError,
    SessionTerminationRequestedError,
    ShellExecutableNotFoundError,
)
from ..oauth.core.context import (
    MissingOAuthScopeError,
    require_oauth_scopes,
)
from ..oauth.core.scopes import SUPPORTED_OAUTH_SCOPES
from ..utils.serialization import to_jsonable
from .contracts import (
    McpToolContext,
    ToolRegistry,
)
from .mcp_text import has_explicit_tool_text, render_tool_text
from .metadata import mcp_security_meta, tool_safety_annotations

type ToolAnnotation = Literal["read_only"]
type ToolDescription = str | Callable[[McpToolContext], str]
type ToolEnabled = Callable[[ControlConfig], bool]
type ToolFunc = Callable[..., Awaitable[Any]]
type McpErrorHandler = Callable[
    [Exception, tuple[Any, ...], dict[str, Any]], Any
]
_MCP_HANDLER_ERROR_HANDLER_ATTR = "__workgate_error_handler__"


def mark_mcp_handler_error_handler(
    handler: Callable[..., Any], error_handler: McpErrorHandler | None
) -> None:
    """Record an optional MCP handler error-to-result conversion strategy."""
    setattr(handler, _MCP_HANDLER_ERROR_HANDLER_ATTR, error_handler)


def mcp_handler_error_handler(
    handler: Callable[..., Any],
) -> McpErrorHandler | None:
    """Return the MCP error-to-result conversion strategy for a handler."""
    value = getattr(handler, _MCP_HANDLER_ERROR_HANDLER_ATTR, None)
    if value is None:
        return None
    return value


class ToolDecoratorFactory(Protocol):
    """Decorator factory for tool registration."""

    def __call__(
        self,
        *,
        name: str | None = None,
        oauth_scopes: tuple[str, ...] | None = None,
        annotations: ToolAnnotation | None = None,
        description: ToolDescription | None = None,
        mcp_error_handler: McpErrorHandler | None = None,
        enabled: ToolEnabled = ...,
    ) -> Callable[[ToolFunc], ToolDefinition]: ...


def _always_enabled(settings: ControlConfig) -> bool:
    return True


def _normalize_description(text: str) -> str:
    """Return a clean MCP tool description from source text. This strips out any leading/trailing whitespace and normalizes paragraph spacing."""
    paragraphs = re.split(r"\n\s*\n", inspect.cleandoc(text))
    return "\n\n".join(
        " ".join(paragraph.split())
        for paragraph in paragraphs
        if paragraph.split()
    )


@dataclass(frozen=True)
class ToolDefinition:
    """Tool definition registered on the canonical MCP surface."""

    func: ToolFunc
    """Typed coroutine that implements the tool's canonical behavior."""
    name: str
    """Canonical MCP tool name."""
    oauth_scopes: tuple[str, ...] | None = None
    """Server-enforced OAuth scopes for this tool. Also drives MCP security metadata."""
    annotations: ToolAnnotation | None = None
    """MCP tool annotations applied during registration."""
    description: ToolDescription | None = None
    """Static or context-derived MCP description override. If not provided, the tool's docstring is used."""
    mcp_error_handler: McpErrorHandler | None = None
    """Optional MCP exception-to-result conversion used for tool errors and timeouts."""
    enabled: ToolEnabled = _always_enabled
    """Predicate controlling whether the tool is exposed for current settings."""

    @property
    def signature(self) -> inspect.Signature:
        """Return the resolved public signature for MCP tool registration."""
        return inspect.signature(self.func, eval_str=True)

    def is_enabled(self, settings: ControlConfig) -> bool:
        """Return whether this tool should be exposed for current settings."""
        return self.enabled(settings)

    def required_oauth_scopes(self) -> tuple[str, ...]:
        """Return server-enforced OAuth scopes for this tool."""
        return self.oauth_scopes or tuple(SUPPORTED_OAUTH_SCOPES)

    def _mcp_annotations(self) -> ToolAnnotations:
        """Build the complete annotation once from the declared tool behavior."""
        match self.annotations:
            case "read_only":
                read_only = True
            case None:
                read_only = False
            case _:
                raise ValueError(f"Invalid annotations: {self.annotations}")
        return tool_safety_annotations(self.name, read_only=read_only)

    def _mcp_description(self, context: McpToolContext) -> str | None:
        if callable(self.description):
            description = self.description(context)
        elif self.description is not None:
            description = self.description
        else:
            description = self.func.__doc__ or ""
        normalized = _normalize_description(description)
        return normalized or None

    def register_mcp(self, mcp: MCPServer, context: McpToolContext) -> None:
        """Register this tool on the provided MCPServer app."""

        @wraps(self.func)
        async def mcp_handler(*args: Any, **kwargs: Any) -> Any:
            try:
                require_oauth_scopes(self.required_oauth_scopes())
                result = await self.func(*args, **kwargs)
                if not has_explicit_tool_text(self.name):
                    return result
                structured = to_jsonable(result)
                text = render_tool_text(self.name, structured)
                if text is None:
                    return result
                return CallToolResult(
                    content=[TextContent(type="text", text=text)],
                    structured_content=structured,
                )
            except SessionTerminationRequestedError as exc:
                raise ToolError(str(exc)) from None
            except MissingOAuthScopeError as exc:
                raise ToolError(str(exc)) from None
            except Exception as exc:
                if self.mcp_error_handler is not None:
                    return self.mcp_error_handler(exc, args, kwargs)
                if isinstance(
                    exc, (PublicToolError, ShellExecutableNotFoundError)
                ):
                    # Display only errors explicitly public or already redacted.
                    raise ToolError(str(exc)) from None
                raise

        mcp_handler.__name__ = self.name
        mcp_handler.__qualname__ = self.name
        mark_mcp_handler_error_handler(mcp_handler, self.mcp_error_handler)
        mcp_handler.__signature__ = self.signature  # type: ignore[attr-defined]
        mcp.tool(
            description=self._mcp_description(context),
            annotations=self._mcp_annotations(),
            meta=mcp_security_meta(
                self.required_oauth_scopes(), settings=context.settings
            ),
            structured_output=True,
        )(mcp_handler)


class DeclarativeToolRegistry(ToolRegistry):
    """Registry base for tool registries that are in a declarative fashion."""

    def __init__(self, settings: ControlConfig | None = None) -> None:
        self._configured_settings = settings
        self._context: McpToolContext | None = None

    tools: ClassVar[tuple[ToolDefinition, ...]] = ()
    """Tool definitions registered on this concrete registry class."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Give every concrete registry its own tool collection."""
        super().__init_subclass__(**kwargs)
        cls.tools = ()

    @classmethod
    def register_tool(cls, tool: ToolDefinition) -> ToolDefinition:
        """Attach a tool definition to this registry class."""
        if any(existing.name == tool.name for existing in cls.tools):
            raise ValueError(f"Duplicate tool definition: {tool.name}")
        cls.tools = (*cls.tools, tool)
        return tool

    @classmethod
    def get_tool_decorator(cls) -> ToolDecoratorFactory:
        """Return a decorator factory that registers tools on this registry."""

        def registry_tool(
            *,
            name: str | None = None,
            oauth_scopes: tuple[str, ...] | None = None,
            annotations: ToolAnnotation | None = None,
            description: ToolDescription | None = None,
            mcp_error_handler: McpErrorHandler | None = None,
            enabled: ToolEnabled = _always_enabled,
        ) -> Callable[[ToolFunc], ToolDefinition]:
            def decorator(func: ToolFunc) -> ToolDefinition:
                return cls.register_tool(
                    ToolDefinition(
                        func=func,
                        name=name or func.__name__,
                        oauth_scopes=oauth_scopes,
                        annotations=annotations,
                        description=description,
                        mcp_error_handler=mcp_error_handler,
                        enabled=enabled,
                    )
                )

            return decorator

        return registry_tool

    def _enabled_tools(self) -> tuple[ToolDefinition, ...]:
        settings = self._settings()
        return tuple(tool for tool in self.tools if tool.is_enabled(settings))

    def _settings(self) -> ControlConfig:
        if self._context is not None:
            return self._context.settings
        if self._configured_settings is not None:
            return self._configured_settings
        return get_control_config()

    def register_mcp(self, mcp: MCPServer, context: McpToolContext) -> None:
        """Register enabled declarative tools on the provided MCPServer app."""
        self._context = context
        try:
            for tool in self._enabled_tools():
                tool.register_mcp(mcp, context)
        finally:
            self._context = None
