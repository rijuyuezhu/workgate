"""Declarative tool registration."""

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any, ClassVar, Literal, Protocol

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import TypeAdapter, ValidationError

from ..config.control import ControlSettingsView
from ..errors import SessionTerminationRequestedError
from ..oauth.core.context import (
    MissingOAuthScopeError,
    require_oauth_scopes,
)
from ..oauth.core.scopes import SUPPORTED_OAUTH_SCOPES
from .contracts import (
    HttpMethod,
    HttpToolRoute,
    McpToolContext,
    ToolHandler,
    ToolRegistry,
)
from .metadata import oauth_security_meta

type McpSecurityProfile = Literal["oauth", "connector_compatible"]
type ToolAnnotation = Literal["read_only"]
type ToolDescription = str | Callable[[McpToolContext], str]
type ToolEnabled = Callable[[ControlSettingsView], bool]
type ToolFunc = Callable[..., Awaitable[Any]]
type McpErrorHandler = Callable[
    [Exception, tuple[Any, ...], dict[str, Any]], Any
]
_MCP_HANDLER_ERROR_HANDLER_ATTR = "__workgate_error_handler__"


def _oauth_scope_http_error(exc: MissingOAuthScopeError) -> HTTPException:
    """Return the transport error shape for one missing OAuth scope."""
    return HTTPException(status_code=403, detail=str(exc))


def _enforce_oauth_scopes(required_scopes: tuple[str, ...]) -> None:
    """Translate OAuth scope failures to the transport error shape."""
    try:
        require_oauth_scopes(required_scopes)
    except MissingOAuthScopeError as exc:
        raise _oauth_scope_http_error(exc) from exc


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


class LocalToolDecoratorFactory(Protocol):
    """Decorator factory for tool registration."""

    def __call__(
        self,
        *,
        http_method: HttpMethod | None,
        http_path: str | None,
        name: str | None = None,
        mcp_security_profile: McpSecurityProfile = "oauth",
        oauth_scopes: tuple[str, ...] | None = None,
        annotations: ToolAnnotation | None = None,
        description: ToolDescription | None = None,
        mcp_error_handler: McpErrorHandler | None = None,
        enabled: ToolEnabled = ...,
        timeout_cancellable: bool = True,
    ) -> Callable[[ToolFunc], ToolDefinition]: ...


def _always_enabled(settings: ControlSettingsView) -> bool:
    return True


def _normalize_description(text: str) -> str:
    """Return a clean MCP tool description from source text. This strips out any leading/trailing whitespace and normalizes paragraph spacing."""
    paragraphs = re.split(r"\n\s*\n", inspect.cleandoc(text))
    return "\n\n".join(
        " ".join(paragraph.split())
        for paragraph in paragraphs
        if paragraph.split()
    )


def _coerce_tool_arg(parameter: inspect.Parameter, value: Any) -> Any:
    """Coerce HTTP-style mapped values through the tool signature annotation."""
    if parameter.annotation is inspect.Parameter.empty:
        return value
    try:
        return TypeAdapter(parameter.annotation).validate_python(value)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _tool_kwargs_from_mapping(
    signature: inspect.Signature, args: Mapping[str, Any]
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if name in args:
            kwargs[name] = _coerce_tool_arg(parameter, args[name])
        elif parameter.default is inspect.Parameter.empty:
            raise ValueError(f"Missing required argument: {name}")
    return kwargs


@dataclass(frozen=True)
class ToolDefinition:
    """Tool definition used to derive MCP and HTTP exposure."""

    func: ToolFunc
    """Typed coroutine that implements the tool's canonical behavior."""
    name: str
    """Tool name exposed to MCP clients and HTTP dispatch."""
    http_method: HttpMethod | None
    """HTTP method for the REST adapter route, or None for MCP-only tools."""
    http_path: str | None
    """HTTP path for the REST adapter route, or None for MCP-only tools."""
    mcp_security_profile: McpSecurityProfile = "oauth"
    """Client-facing MCP securitySchemes profile advertised for this tool."""
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
    timeout_cancellable: bool = True
    """Whether the REST watchdog may cancel this tool on timeout."""

    @property
    def signature(self) -> inspect.Signature:
        """Return the fully resolved public signature advertised to adapters."""
        return inspect.signature(self.func, eval_str=True)

    def is_enabled(self, settings: ControlSettingsView) -> bool:
        """Return whether this tool should be exposed for current settings."""
        return self.enabled(settings)

    def http_route(self) -> HttpToolRoute | None:
        """Return the HTTP route for this tool, if it has one."""
        if self.http_method is None or self.http_path is None:
            return None
        return HttpToolRoute(
            self.http_method,
            self.http_path,
            self.name,
            timeout_cancellable=self.timeout_cancellable,
        )

    def required_oauth_scopes(self) -> tuple[str, ...]:
        """Return server-enforced OAuth scopes for this tool."""
        return self.oauth_scopes or tuple(SUPPORTED_OAUTH_SCOPES)

    async def call_from_mapping(self, args: Mapping[str, Any]) -> Any:
        """Invoke the typed tool function from an HTTP-style argument mapping."""
        _enforce_oauth_scopes(self.required_oauth_scopes())
        try:
            return await self.func(
                **_tool_kwargs_from_mapping(self.signature, args)
            )
        except MissingOAuthScopeError as exc:
            raise _oauth_scope_http_error(exc) from exc

    def http_handler(self) -> Callable[[dict[str, Any]], Awaitable[Any]]:
        """Return a HTTP invocation handler for this tool."""

        async def handler(args: dict[str, Any]) -> Any:
            return await self.call_from_mapping(args)

        return handler

    def _mcp_security_meta(self) -> dict[str, Any]:
        match self.mcp_security_profile:
            case "connector_compatible" | "oauth":
                return oauth_security_meta(
                    self.required_oauth_scopes(),
                    connector_compatible=(
                        self.mcp_security_profile == "connector_compatible"
                    ),
                )
            case _:
                raise ValueError(
                    f"Invalid MCP security profile: {self.mcp_security_profile}"
                )

    def _mcp_annotations(
        self, context: McpToolContext
    ) -> ToolAnnotations | None:
        match self.annotations:
            case "read_only":
                return context.read_only_tool_annotations
            case None:
                return None
            case _:
                raise ValueError(f"Invalid annotations: {self.annotations}")

    def _mcp_description(self, context: McpToolContext) -> str | None:
        if callable(self.description):
            description = self.description(context)
        elif self.description is not None:
            description = self.description
        else:
            description = self.func.__doc__ or ""
        normalized = _normalize_description(description)
        return normalized or None

    def register_mcp(self, mcp: FastMCP, context: McpToolContext) -> None:
        """Register this tool on the provided FastMCP app."""

        @wraps(self.func)
        async def mcp_handler(*args: Any, **kwargs: Any) -> Any:
            try:
                _enforce_oauth_scopes(self.required_oauth_scopes())
                return await self.func(*args, **kwargs)
            except SessionTerminationRequestedError:
                raise
            except MissingOAuthScopeError as exc:
                raise _oauth_scope_http_error(exc) from exc
            except Exception as exc:
                if self.mcp_error_handler is not None:
                    return self.mcp_error_handler(exc, args, kwargs)
                raise

        mcp_handler.__name__ = self.name
        mcp_handler.__qualname__ = self.name
        mark_mcp_handler_error_handler(mcp_handler, self.mcp_error_handler)
        mcp_handler.__signature__ = self.signature  # type: ignore[attr-defined]
        mcp.tool(
            description=self._mcp_description(context),
            annotations=self._mcp_annotations(context),
            meta=self._mcp_security_meta(),
            structured_output=True,
        )(mcp_handler)


class DeclarativeToolRegistry(ToolRegistry):
    """Registry base for tool registries that are in a declarative fashion."""

    def __init__(self, settings: ControlSettingsView | None = None) -> None:
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
    def get_tool_decorator(cls) -> LocalToolDecoratorFactory:
        """Return a decorator factory that registers tools on this registry."""

        def registry_local_tool(
            *,
            http_method: HttpMethod | None,
            http_path: str | None,
            name: str | None = None,
            mcp_security_profile: McpSecurityProfile = "oauth",
            oauth_scopes: tuple[str, ...] | None = None,
            annotations: ToolAnnotation | None = None,
            description: ToolDescription | None = None,
            mcp_error_handler: McpErrorHandler | None = None,
            enabled: ToolEnabled = _always_enabled,
            timeout_cancellable: bool = True,
        ) -> Callable[[ToolFunc], ToolDefinition]:
            def decorator(func: ToolFunc) -> ToolDefinition:
                return cls.register_tool(
                    ToolDefinition(
                        func=func,
                        name=name or func.__name__,
                        http_method=http_method,
                        http_path=http_path,
                        mcp_security_profile=mcp_security_profile,
                        oauth_scopes=oauth_scopes,
                        annotations=annotations,
                        description=description,
                        mcp_error_handler=mcp_error_handler,
                        enabled=enabled,
                        timeout_cancellable=timeout_cancellable,
                    )
                )

            return decorator

        return registry_local_tool

    def _enabled_tools(self) -> tuple[ToolDefinition, ...]:
        settings = self._settings()
        return tuple(tool for tool in self.tools if tool.is_enabled(settings))

    def _settings(self) -> ControlSettingsView:
        if self._context is not None:
            return self._context.settings
        if self._configured_settings is not None:
            return self._configured_settings
        from ..config.settings import get_settings

        return get_settings()

    def http_routes(self) -> Iterable[HttpToolRoute]:
        """Return enabled declarative REST routes."""
        return tuple(
            route
            for tool in self._enabled_tools()
            if (route := tool.http_route()) is not None
        )

    def http_handlers(
        self,
    ) -> Mapping[str, ToolHandler]:
        """Return enabled declarative HTTP handlers keyed by tool name."""
        return {
            tool.name: tool.http_handler() for tool in self._enabled_tools()
        }

    def register_mcp(self, mcp: FastMCP, context: McpToolContext) -> None:
        """Register enabled declarative tools on the provided FastMCP app."""
        self._context = context
        try:
            for tool in self._enabled_tools():
                tool.register_mcp(mcp, context)
        finally:
            self._context = None
