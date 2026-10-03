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
    return """Check and apply a unified diff or apply_patch envelope inside an execution session. Paths must stay inside cwd. The tool validates the envelope and runs `git apply --check` before applying it; prefer hashline_edit for ordinary grounded edits."""


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
