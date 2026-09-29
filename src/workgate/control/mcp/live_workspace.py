"""Session-scoped Live Workspace MCP App composition."""

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ...audit import current_audit_call_id, query_audit
from ...oauth.core.context import require_oauth_scopes
from ...oauth.core.scopes import (
    SCOPE_AUDIT_READ,
    SCOPE_SHELL_EXECUTE,
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.input_models.task import ExpectedTaskRevisionArg
from ...schemas.result_models.live_workspace import (
    LiveWorkspaceActivity,
    LiveWorkspaceJob,
    LiveWorkspaceLinks,
    LiveWorkspaceSession,
    LiveWorkspaceShell,
    LiveWorkspaceSnapshot,
)
from ...schemas.result_models.session import SessionEndOutput
from ...schemas.result_models.task import SessionTaskDocument, SessionTaskOutput
from ...tools.metadata import oauth_security_meta
from ..runtime import ControlRuntime

_RESOURCE_PATH = Path(__file__).with_name("live_workspace.html")
_RESOURCE_URI = "ui://workgate/live-workspace.html"
_RESOURCE_MIME = "text/html;profile=mcp-app"
_ACTIVITY_LIMIT = 24
_ACTIVITY_SCAN_LIMIT = _ACTIVITY_LIMIT * 4
_JOB_LIMIT = 24
_SHELL_LIMIT = 32

TaskAction = Literal["block", "resume", "cancel", "next_instruction"]


def _resource_versioned_uri() -> str:
    try:
        digest = hashlib.sha256(_RESOURCE_PATH.read_bytes()).hexdigest()[:16]
    except OSError:
        digest = "unbuilt"
    return f"ui://workgate/live-workspace-{digest}.html"


def _resource_html() -> str:
    try:
        return _RESOURCE_PATH.read_text(encoding="utf-8")
    except OSError:
        return """<!doctype html><html><body><strong>Workgate Live Workspace asset is unavailable.</strong></body></html>"""


def _resource_meta(runtime: ControlRuntime) -> dict[str, Any]:
    parsed = urlparse(runtime.config.resolved_base_url)
    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else ""
    )
    return {
        "ui": {
            "domain": origin,
            "csp": {"connectDomains": []},
            "prefersBorder": False,
        },
        "openai/widgetDescription": (
            "A session-scoped Workgate Live Workspace showing task state, "
            "jobs, shells, recent activity, and safe human controls."
        ),
        "openai/widgetDomain": origin,
        "openai/widgetPrefersBorder": False,
    }


def _read_only_annotations() -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _mutating_annotations(*, destructive: bool) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=destructive,
        idempotentHint=False,
        openWorldHint=False,
    )


def _app_meta(
    scopes: tuple[str, ...], *, resource_uri: str | None = None
) -> dict[str, Any]:
    ui: dict[str, Any] = {"visibility": ["app"]}
    meta: dict[str, Any] = {**oauth_security_meta(scopes), "ui": ui}
    if resource_uri is not None:
        ui["resourceUri"] = resource_uri
        meta.update(
            {
                "ui/resourceUri": resource_uri,
                "openai/outputTemplate": resource_uri,
                "openai/widgetAccessible": True,
                "openai/toolInvocation/invoking": "Opening Live Workspace",
                "openai/toolInvocation/invoked": "Live Workspace ready",
            }
        )
        # workspace_open itself must remain model-visible.
        ui.pop("visibility", None)
    return meta


def _human_ui_links(
    runtime: ControlRuntime,
    *,
    session_id: str,
    executor_id: str,
    workdir: str,
    shell_id: str | None,
) -> LiveWorkspaceLinks:
    base = runtime.config.resolved_base_url.rstrip("/")
    ui_path = runtime.config.ui_path
    if not ui_path.startswith("/"):
        ui_path = "/" + ui_path
    root = f"{base}{ui_path}"

    def link(view: str, **extra: str | None) -> str:
        params = {
            "session_id": session_id,
            "executor_id": executor_id,
            **{key: value for key, value in extra.items() if value},
        }
        return f"{root}?{urlencode(params)}#{view}"

    return LiveWorkspaceLinks(
        sessions=link("sessions"),
        files=link("files", workdir=workdir),
        terminals=link("terminals", shell_id=shell_id),
        audit=link("audit"),
    )


def _safe_activity(raw: dict[str, Any]) -> LiveWorkspaceActivity:
    return LiveWorkspaceActivity(
        id=str(raw["id"]) if raw.get("id") is not None else None,
        ts=float(raw["ts"]) if isinstance(raw.get("ts"), int | float) else None,
        event=str(raw["event"]) if raw.get("event") is not None else None,
        tool=str(raw["tool"]) if raw.get("tool") is not None else None,
        operation=(
            str(raw["operation"]) if raw.get("operation") is not None else None
        ),
        ok=raw.get("ok") if isinstance(raw.get("ok"), bool) else None,
        duration_ms=(
            float(raw["duration_ms"])
            if isinstance(raw.get("duration_ms"), int | float)
            else None
        ),
    )


async def _task_projection(
    runtime: ControlRuntime, session_id: str
) -> SessionTaskOutput:
    return await runtime.task_service.read_task(session_id)


def _task_control_actions(
    task: SessionTaskDocument,
    *,
    execution_status: str,
) -> tuple[list[TaskAction], str | None]:
    if execution_status != "active":
        return (
            [],
            f"Task controls are unavailable while session is {execution_status}.",
        )
    task_status = task.status
    if task_status == "active":
        return ["block", "cancel", "next_instruction"], None
    if task_status == "blocked":
        return ["resume", "cancel", "next_instruction"], None
    if task_status == "completed":
        return [
            "resume"
        ], "Task is completed; resume it before making other changes."
    if task_status == "cancelled":
        return [], "Task is cancelled and terminal."
    return [], f"Task controls are unavailable for task status {task_status!r}."


async def _job_projection(
    runtime: ControlRuntime, session_id: str, status: str
) -> tuple[list[LiveWorkspaceJob], str | None]:
    if status not in {"active", "terminating"}:
        return (
            [],
            "Active jobs are unavailable after the execution session ends.",
        )
    try:
        output = await runtime.job_service.execute(
            session_id=session_id,
            list_jobs=True,
            include_finished=False,
            lines=1,
        )
    except Exception:
        return [], "Job snapshot unavailable."
    rows = output.jobs[:_JOB_LIMIT]
    if len(output.jobs) > _JOB_LIMIT:
        message = f"Showing {_JOB_LIMIT} most recent jobs."
    elif output.message and output.message.startswith(
        "Executor jobs unavailable"
    ):
        # ControlJobService may append backend exception text to this diagnostic.
        # Keep the compact App on an explicit allow-list boundary instead of
        # forwarding arbitrary executor/control error strings.
        message = "Some executor job metadata is unavailable."
    else:
        message = None
    return (
        [
            LiveWorkspaceJob(
                job_id=item.job_id,
                kind=item.kind,
                name=item.name,
                status=item.status,
                cwd=item.cwd,
                created_at=item.created_at,
                updated_at=item.updated_at,
                completed_at=item.completed_at,
                attempts=item.attempts,
            )
            for item in rows
        ],
        message,
    )


async def _shell_projection(
    runtime: ControlRuntime, session_id: str, availability: str
) -> tuple[list[LiveWorkspaceShell], str | None]:
    if availability != "available":
        return (
            [],
            f"Persistent shells unavailable while session is {availability}.",
        )
    try:
        raw = await runtime.session_coordinator.call_session_tool(
            "list_persistent_shells", {"session_id": session_id}
        )
    except Exception:
        return [], "Persistent-shell snapshot unavailable."
    shells_value = raw.get("shells") if isinstance(raw, dict) else None
    shells: list[Any] = shells_value if isinstance(shells_value, list) else []
    result = [
        LiveWorkspaceShell(
            shell_id=str(item.get("shell_id") or ""),
            name=(str(item["name"]) if item.get("name") is not None else None),
            cwd=(str(item["cwd"]) if item.get("cwd") is not None else None),
            backend=(
                str(item["backend"])
                if item.get("backend") is not None
                else None
            ),
        )
        for item in shells[:_SHELL_LIMIT]
        if isinstance(item, dict) and item.get("shell_id")
    ]
    message = (
        f"Showing {_SHELL_LIMIT} active persistent shells."
        if len(shells) > _SHELL_LIMIT
        else None
    )
    return result, message


async def _activity_projection(session_id: str) -> list[LiveWorkspaceActivity]:
    # The public ControlAuditService intentionally admits only active/available
    # sessions. Live Workspace must retain bounded durable activity while the
    # executor is offline or the execution session has ended, so query the same
    # canonical audit store directly after the caller session was resolved.
    raw = await asyncio.to_thread(
        query_audit,
        limit=_ACTIVITY_SCAN_LIMIT,
        session=session_id,
        sort="desc",
        exclude_call_id=current_audit_call_id(),
    )
    entries = raw.get("entries", []) if isinstance(raw, dict) else []
    visible = [
        item
        for item in entries
        if isinstance(item, dict) and item.get("tool") != "workspace_snapshot"
    ]
    return [_safe_activity(item) for item in visible[:_ACTIVITY_LIMIT]]


async def live_workspace_snapshot(
    runtime: ControlRuntime, session_id: str
) -> LiveWorkspaceSnapshot:
    """Reconstruct one Live Workspace solely from canonical session state."""

    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None:
        raise ValueError(
            f"unknown session_id {session_id!r}; call session_start first"
        )

    (
        availability,
        last_active_at,
    ) = await runtime.session_coordinator.session_activity_projection(
        session_id
    )
    executor = runtime.control_state.snapshot_executors().get(
        str(record.executor_id)
    )

    task_result, jobs_result, shells_result, activity = await asyncio.gather(
        _task_projection(runtime, session_id),
        _job_projection(runtime, session_id, str(record.status)),
        _shell_projection(runtime, session_id, availability),
        _activity_projection(session_id),
    )
    task = task_result
    jobs, jobs_message = jobs_result
    shells, shells_message = shells_result
    task_actions, controls_message = _task_control_actions(
        task,
        execution_status=str(record.status),
    )

    return LiveWorkspaceSnapshot(
        session=LiveWorkspaceSession(
            session_id=session_id,
            label=record.label,
            executor_id=str(record.executor_id),
            executor_name=executor.name if executor is not None else None,
            workdir=record.resolved_workdir_display or record.requested_workdir,
            status=str(record.status),
            availability=availability,
            created_at=float(record.created_at),
            updated_at=float(record.updated_at),
            last_active_at=last_active_at,
        ),
        task=task,
        task_control_actions=task_actions,
        task_controls_message=controls_message,
        jobs=jobs,
        jobs_message=jobs_message,
        shells=shells,
        shells_message=shells_message,
        activity=activity,
        links=_human_ui_links(
            runtime,
            session_id=session_id,
            executor_id=str(record.executor_id),
            # Deep links must follow the session's already-resolved binding, not
            # re-resolve the user's original cwd spelling later.
            workdir=record.resolved_workdir_display or record.requested_workdir,
            shell_id=shells[0].shell_id if shells else None,
        ),
    )


async def live_workspace_task_control(
    runtime: ControlRuntime,
    *,
    session_id: str,
    action: TaskAction,
    expected_revision: ExpectedTaskRevisionArg,
    instruction: str | None = None,
) -> LiveWorkspaceSnapshot:
    """Apply one semantic mutation to the canonical session task state."""

    record = runtime.control_state.snapshot_sessions().get(session_id)
    if record is None:
        raise ValueError(
            f"unknown session_id {session_id!r}; call session_start first"
        )
    current_task = await runtime.task_service.read_task(session_id)
    allowed_actions, state_message = _task_control_actions(
        current_task,
        execution_status=str(record.status),
    )
    if action not in allowed_actions:
        detail = state_message or (
            "allowed actions: " + ", ".join(allowed_actions)
            if allowed_actions
            else "no task controls are currently available"
        )
        raise ValueError(f"task action {action!r} is not available: {detail}")

    kwargs: dict[str, Any] = {"expected_revision": expected_revision}
    match action:
        case "block":
            kwargs["task_status"] = "blocked"
        case "resume":
            kwargs["task_status"] = "active"
        case "cancel":
            kwargs["task_status"] = "cancelled"
        case "next_instruction":
            note = str(instruction or "").strip()
            if not note:
                raise ValueError("instruction is required for next_instruction")
            kwargs["next_action"] = note
        case _:
            raise ValueError(
                f"unsupported Live Workspace task action: {action}"
            )

    await runtime.task_service.report_progress(session_id, **kwargs)
    return await live_workspace_snapshot(runtime, session_id)


async def live_workspace_end(
    runtime: ControlRuntime,
    *,
    session_id: str,
    confirm_session_id: str,
) -> SessionEndOutput:
    """End only when the human confirmation repeats the exact session id."""

    if confirm_session_id != session_id:
        raise ValueError("confirm_session_id must exactly match session_id")
    result = await runtime.session_coordinator.end_session(
        session_id, force=False
    )
    return SessionEndOutput.model_validate(result)


def register_live_workspace(
    mcp: FastMCP, runtime: ControlRuntime | None
) -> None:
    """Register the HTTP-only session-scoped MCP App on a routed runtime."""

    if (
        runtime is None
        or runtime.config.mode == "stdio"
        or not runtime.config.ui_enabled
    ):
        return

    versioned_uri = _resource_versioned_uri()
    resource_options = {
        "name": "workgate-live-workspace",
        "title": "Workgate Live Workspace",
        "description": "Session-scoped human view and controls for one Workgate session.",
        "mime_type": _RESOURCE_MIME,
        "meta": _resource_meta(runtime),
    }

    @mcp.resource(_RESOURCE_URI, **resource_options)
    def live_workspace_resource() -> str:
        return _resource_html()

    @mcp.resource(versioned_uri, **resource_options)
    def versioned_live_workspace_resource() -> str:
        return _resource_html()

    read_scopes = (SCOPE_SHELL_READ, SCOPE_AUDIT_READ)
    task_write_scopes = (SCOPE_SHELL_READ, SCOPE_SHELL_WRITE, SCOPE_AUDIT_READ)

    @mcp.tool(
        description=(
            "Open the Live Workspace for one explicit existing Workgate session_id. "
            "This never creates, lists, guesses, rebinds, or revives a session."
        ),
        annotations=_read_only_annotations(),
        meta=_app_meta(read_scopes, resource_uri=versioned_uri),
        structured_output=True,
    )
    async def workspace_open(session_id: SessionIdArg) -> LiveWorkspaceSnapshot:
        require_oauth_scopes(read_scopes)
        return await live_workspace_snapshot(runtime, str(session_id))

    @mcp.tool(
        description="Refresh the MCP App snapshot for the same explicit Workgate session.",
        annotations=_read_only_annotations(),
        meta=_app_meta(read_scopes),
        structured_output=True,
    )
    async def workspace_snapshot(
        session_id: SessionIdArg,
    ) -> LiveWorkspaceSnapshot:
        require_oauth_scopes(read_scopes)
        return await live_workspace_snapshot(runtime, str(session_id))

    @mcp.tool(
        description="Apply one safe semantic task control from the Live Workspace.",
        annotations=_mutating_annotations(destructive=True),
        meta=_app_meta(task_write_scopes),
        structured_output=True,
    )
    async def workspace_task_control(
        session_id: SessionIdArg,
        action: TaskAction,
        expected_revision: ExpectedTaskRevisionArg,
        instruction: str | None = None,
    ) -> LiveWorkspaceSnapshot:
        require_oauth_scopes(task_write_scopes)
        return await live_workspace_task_control(
            runtime,
            session_id=str(session_id),
            action=action,
            expected_revision=expected_revision,
            instruction=instruction,
        )

    @mcp.tool(
        description=(
            "End the explicit Live Workspace session after a separate human confirmation. "
            "confirm_session_id must repeat session_id exactly."
        ),
        annotations=_mutating_annotations(destructive=True),
        meta=_app_meta((SCOPE_SHELL_EXECUTE,)),
        structured_output=True,
    )
    async def workspace_end(
        session_id: SessionIdArg,
        confirm_session_id: str,
    ) -> SessionEndOutput:
        require_oauth_scopes((SCOPE_SHELL_EXECUTE,))
        return await live_workspace_end(
            runtime,
            session_id=str(session_id),
            confirm_session_id=confirm_session_id,
        )
