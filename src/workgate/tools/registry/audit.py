"""Session-bound audit MCP tool registry."""

from ...oauth.core.scopes import SCOPE_AUDIT_FULL, SCOPE_AUDIT_READ
from ...schemas.input_models.session import SessionIdArg
from ..declarative import DeclarativeToolRegistry, _enforce_oauth_scopes
from ..ops.audit import audit_tail_execute
from ..schemas.input_models.audit import (
    AuditEntryIdArg,
    AuditEventArg,
    AuditIncludeFullPayloadsArg,
    AuditLimitArg,
    AuditOperationArg,
    AuditSearchArg,
    AuditSessionArg,
    AuditSortArg,
    AuditTimestampArg,
)
from ..schemas.result_models.audit import AuditTailOutput


class AuditToolRegistry(DeclarativeToolRegistry):
    """Register session-bound audit tools."""

    name = "audit"
    """Registry group name used for tool-surface organization."""


audit_tool = AuditToolRegistry.get_tool_decorator()


@audit_tool(
    http_method="GET",
    http_path="/tools/audit_tail",
    annotations="read_only",
    oauth_scopes=(SCOPE_AUDIT_READ,),
)
async def audit_tail(
    session_id: SessionIdArg,
    limit: AuditLimitArg = 100,
    event: AuditEventArg = None,
    operation: AuditOperationArg = None,
    audit_session: AuditSessionArg = None,
    search: AuditSearchArg = None,
    start_ts: AuditTimestampArg = None,
    end_ts: AuditTimestampArg = None,
    sort: AuditSortArg = "desc",
    entry_id: AuditEntryIdArg = None,
    include_full_payloads: AuditIncludeFullPayloadsArg = False,
) -> AuditTailOutput:
    """Read bounded, coalesced audit history for the executor-backed target selected by an explicit shared agent session. Listing and previews require audit:read. Set entry_id to retrieve one logical entry; set include_full_payloads=true only with entry_id to resolve retained sanitized payloads, which additionally requires audit:full. Searches cover metadata rather than stored payload bodies. The current audit_tail lifecycle is excluded from its own stable read snapshot."""
    if include_full_payloads:
        _enforce_oauth_scopes((SCOPE_AUDIT_FULL,))
    return await audit_tail_execute(
        session_id,
        limit,
        event,
        operation,
        audit_session,
        search,
        start_ts,
        end_ts,
        sort,
        entry_id,
        include_full_payloads,
    )
