"""Canonical configuration schema shared independently by both runtime roles."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SharedRoleConfig:
    """Policy shape present independently in both control and executor roles."""

    state_dir: Path
    ui_terminal_idle_timeout_s: int
    ui_terminal_max_connections: int
    max_job_log_bytes: int
    max_jobs: int
    max_view_image_bytes: int
    max_audit_log_bytes: int
    max_audit_event_bytes: int
    audit_payloads_enabled: bool
    audit_inline_value_bytes: int
    max_audit_payload_bytes: int
    max_audit_payload_store_bytes: int
    audit_payload_retention_s: int
    agent_mcp_probe_timeout_s: int
    agent_mcp_call_timeout_s: int


_ROLE_CONFIG_CONTEXT: ContextVar[SharedRoleConfig | None] = ContextVar(
    "workgate_role_config_context", default=None
)


@contextmanager
def use_role_config(config: SharedRoleConfig) -> Generator[None]:
    """Bind one resolved role config to the current execution context."""
    token = _ROLE_CONFIG_CONTEXT.set(config)
    try:
        yield
    finally:
        _ROLE_CONFIG_CONTEXT.reset(token)


def current_role_config() -> SharedRoleConfig | None:
    """Return the explicitly bound role config, if a runtime owns this scope."""
    return _ROLE_CONFIG_CONTEXT.get()


def resolve_shared_role_config(settings) -> SharedRoleConfig:
    """Snapshot shared policy from the transitional flat Settings surface."""
    return SharedRoleConfig(
        state_dir=settings.state_dir.resolve(strict=False),
        ui_terminal_idle_timeout_s=settings.ui_terminal_idle_timeout_s,
        ui_terminal_max_connections=settings.ui_terminal_max_connections,
        max_job_log_bytes=settings.max_job_log_bytes,
        max_jobs=settings.max_jobs,
        max_view_image_bytes=settings.max_view_image_bytes,
        max_audit_log_bytes=settings.max_audit_log_bytes,
        max_audit_event_bytes=settings.max_audit_event_bytes,
        audit_payloads_enabled=settings.audit_payloads_enabled,
        audit_inline_value_bytes=settings.audit_inline_value_bytes,
        max_audit_payload_bytes=settings.max_audit_payload_bytes,
        max_audit_payload_store_bytes=settings.max_audit_payload_store_bytes,
        audit_payload_retention_s=settings.audit_payload_retention_s,
        agent_mcp_probe_timeout_s=settings.agent_mcp_probe_timeout_s,
        agent_mcp_call_timeout_s=settings.agent_mcp_call_timeout_s,
    )


def get_role_config() -> SharedRoleConfig:
    """Return scoped role policy, or resolve a bootstrap compatibility view."""
    config = current_role_config()
    if config is not None:
        return config
    from .settings import get_settings

    return resolve_shared_role_config(get_settings())
