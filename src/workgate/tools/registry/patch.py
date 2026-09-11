"""Patch application tool registry."""

from ...schemas.input_models.patch import PatchCwdArg, PatchTextArg
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.patch import ApplyPatchOutput
from ..contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry


class PatchToolRegistry(DeclarativeToolRegistry):
    """Register compatibility patch application tools."""

    name = "patch"
    """Stable registry name used for discovery and diagnostics."""


patch_tool = PatchToolRegistry.get_tool_decorator()


def _apply_patch_description(context: McpToolContext) -> str:
    del context
    return """Check and apply a standard unified diff or an apply_patch envelope inside an explicit agent/workspace session. Paths resolve relative to cwd within the session workdir; absolute envelope paths are accepted only when they stay inside cwd. The tool validates the entire envelope, runs `git apply --check`, and applies only after preflight succeeds. Prefer hashline_edit for ordinary grounded edits copied from read/search; use apply_patch for portable multi-file patches or compatibility with apply_patch envelopes. The bound executor applies its configured patch/write limit."""


@patch_tool(
    http_method="POST",
    http_path="/tools/apply_patch",
    description=_apply_patch_description,
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def apply_patch(
    session_id: SessionIdArg,
    patch: PatchTextArg,
    cwd: PatchCwdArg = ".",
) -> ApplyPatchOutput:
    """Validate and apply a unified diff or apply_patch envelope."""
    del session_id, patch, cwd
    raise RuntimeError("apply_patch requires control routing")
