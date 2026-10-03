"""Task/session audit MCP tool registry."""

from ...oauth.core.scopes import SCOPE_AUDIT_READ
from ...schemas.input_models.session import OptionalSessionIdArg
from ...schemas.input_models.task import OptionalTaskIdArg
from ..declarative import DeclarativeToolRegistry
from ..schemas.input_models.audit import (
    AuditEntryIdArg,
    AuditEventArg,
    AuditIncludeFullPayloadsArg,
    AuditLimitArg,
    AuditOperationArg,
    AuditSearchArg,
    AuditSortArg,
    AuditTimestampArg,
)
from ..schemas.result_models.audit import AuditTailOutput


class AuditToolRegistry(DeclarativeToolRegistry):
    """Register canonical audit query tools."""

    name = "audit"


audit_tool = AuditToolRegistry.get_tool_decorator()


@audit_tool(
    http_method="GET",
    http_path="/tools/audit_tail",
    annotations="read_only",
    oauth_scopes=(SCOPE_AUDIT_READ,),
)
async def audit_tail(
    task_id: OptionalTaskIdArg = None,
    session_id: OptionalSessionIdArg = None,
    limit: AuditLimitArg = 100,
    event: AuditEventArg = None,
    operation: AuditOperationArg = None,
    search: AuditSearchArg = None,
    start_ts: AuditTimestampArg = None,
    end_ts: AuditTimestampArg = None,
    sort: AuditSortArg = "desc",
    entry_id: AuditEntryIdArg = None,
    include_full_payloads: AuditIncludeFullPayloadsArg = False,
) -> AuditTailOutput:
    """Read bounded audit history by task_id, session_id, or both. At least one is required."""
    del (
        task_id,
        session_id,
        limit,
        event,
        operation,
        search,
        start_ts,
        end_ts,
        sort,
        entry_id,
        include_full_payloads,
    )
    raise RuntimeError("audit_tail requires control routing")
