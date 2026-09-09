"""Session-bound unified-diff and apply_patch envelope operations."""

import asyncio
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from ...schemas.result_models.patch import ApplyPatchOutput
from ...tool_session.store import ToolSessionStore
from ...utils.path_policy import relative_display_from_root
from ..config import ExecutorConfig
from ..path import assert_text_input_size
from ..temp_file import write_temp_text_file
from .envelope import git_apply_prefix, normalize_patch_text

APPLY_PATCH_PHASE_TIMEOUT_S = 20
APPLY_PATCH_WATCHDOG_TIMEOUT_S = 45
_APPLY_PATCH_MAX_OUTPUT_BYTES = 500_000


def _bounded_text(data: bytes, limit: int) -> tuple[str, bool]:
    """Decode one bounded subprocess stream and report tail truncation."""
    truncated = len(data) > limit
    payload = data[-limit:] if truncated else data
    return payload.decode("utf-8", errors="replace"), truncated


def _git_apply_args(
    git_bin: str,
    patch_path: Path,
    prefix: str | None,
    *,
    check: bool,
) -> list[str]:
    args = [git_bin, "apply"]
    if check:
        args.append("--check")
    if prefix:
        args.extend(["--directory", prefix])
    args.append(str(patch_path))
    return args


def _run_git_apply(
    args: list[str],
    cwd: Path,
    *,
    timeout_s: int = APPLY_PATCH_PHASE_TIMEOUT_S,
) -> dict[str, Any]:
    """Run one bounded git apply phase without shell interpolation."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout_s,
        )
        stdout, stdout_truncated = _bounded_text(
            completed.stdout, _APPLY_PATCH_MAX_OUTPUT_BYTES
        )
        stderr, stderr_truncated = _bounded_text(
            completed.stderr, _APPLY_PATCH_MAX_OUTPUT_BYTES
        )
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "command": shlex.join(args),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = _bounded_text(
            exc.stdout or b"", _APPLY_PATCH_MAX_OUTPUT_BYTES
        )
        stderr, stderr_truncated = _bounded_text(
            exc.stderr or b"", _APPLY_PATCH_MAX_OUTPUT_BYTES
        )
        message = f"git apply timed out after {timeout_s} seconds"
        stderr = f"{stderr}\n{message}".strip()
        return {
            "ok": False,
            "exit_code": None,
            "timed_out": True,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "command": shlex.join(args),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }


async def apply_patch_execute(
    config: ExecutorConfig,
    store: ToolSessionStore,
    patch: str,
    cwd: str,
    session_id: str,
) -> ApplyPatchOutput:
    """Validate and apply one patch under explicit executor/session authority."""
    assert_text_input_size("patch", patch, config.max_file_write_bytes)
    session = store.touch_session(session_id)
    resolved_cwd = store.resolve_session_path(session, cwd, must_exist=True)
    if not resolved_cwd.is_dir():
        raise NotADirectoryError(str(resolved_cwd))

    normalized = await asyncio.to_thread(
        normalize_patch_text, patch, str(resolved_cwd)
    )
    patch_path = await write_temp_text_file(
        "patch",
        normalized,
        filename_prefix="patch",
        suffix="diff",
        max_input_bytes=config.max_file_write_bytes,
        max_tmp_files=config.max_tmp_files,
        max_tmp_bytes=config.max_tmp_bytes,
        temp_directory=config.temp_dir,
    )
    prefix = await asyncio.to_thread(
        git_apply_prefix, config.git_bin, str(resolved_cwd)
    )
    check_args = _git_apply_args(config.git_bin, patch_path, prefix, check=True)
    check = await asyncio.to_thread(_run_git_apply, check_args, resolved_cwd)
    display_cwd = relative_display_from_root(
        resolved_cwd, config.workspace_root
    )
    display_patch = relative_display_from_root(
        patch_path, config.workspace_root
    )
    if not check["ok"]:
        return ApplyPatchOutput(
            **check,
            cwd=display_cwd,
            patch_path=display_patch,
            checked=False,
            applied=False,
        )

    apply_args = _git_apply_args(
        config.git_bin, patch_path, prefix, check=False
    )
    result = await asyncio.to_thread(_run_git_apply, apply_args, resolved_cwd)
    return ApplyPatchOutput(
        **result,
        cwd=display_cwd,
        patch_path=display_patch,
        checked=True,
        applied=bool(result["ok"]),
    )
