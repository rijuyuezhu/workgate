import subprocess
from pathlib import Path

import pytest

import workgate.executor.patch.envelope as patch_ops
from workgate.config.settings import Settings
from workgate.executor.config import ExecutorConfig
from workgate.executor.patch import (
    APPLY_PATCH_PHASE_TIMEOUT_S,
    _git_apply_args,
    _run_git_apply,
    apply_patch_execute,
)
from workgate.executor.runtime import build_executor_runtime
from workgate.tool_session.store import ToolSessionStore


def test_patch_git_subprocesses_detach_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object):
        calls.append((args, kwargs))
        text_mode = bool(kwargs.get("text"))
        stdout = "nested/\n" if text_mode else b""
        stderr = "" if text_mode else b""
        return subprocess.CompletedProcess(args, 0, stdout, stderr)

    monkeypatch.setattr(patch_ops.subprocess, "run", fake_run)
    monkeypatch.setattr("workgate.executor.patch.subprocess.run", fake_run)

    assert patch_ops.git_apply_prefix("git", str(tmp_path)) == "nested"
    result = _run_git_apply(["git", "apply", "patch.diff"], tmp_path)

    assert result["ok"] is True
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["timeout"] == 5
    assert calls[1][1]["stdin"] is subprocess.DEVNULL
    assert calls[1][1]["timeout"] == APPLY_PATCH_PHASE_TIMEOUT_S


def _executor_session(
    workspace: Path,
    state_dir: Path,
) -> tuple[Settings, ExecutorConfig, ToolSessionStore, str]:
    settings = Settings(
        workspace_root=workspace,
        state_dir=state_dir,
        remote_enabled=False,
        agent_bridge_enabled=False,
    )
    runtime = build_executor_runtime(settings, enable_control_connection=False)
    session_id = "sess_0000000000000000000001"
    runtime.services.tool_session_store.create_session(
        session_id=session_id,
        workdir=workspace,
    )
    return (
        settings,
        runtime.config,
        runtime.services.tool_session_store,
        session_id,
    )


@pytest.mark.asyncio
async def test_apply_patch_envelope_is_session_bound_and_atomic(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "sample.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    _, config, store, session_id = _executor_session(
        workspace, tmp_path / "state"
    )
    patch = """*** Begin Patch
*** Update File: sample.txt
@@
 one
-two
+TWO
*** Add File: added.txt
+new
*** End Patch
"""

    result = await apply_patch_execute(config, store, patch, ".", session_id)

    assert result.ok is True
    assert result.checked is True
    assert result.applied is True
    assert target.read_bytes() == b"one\r\nTWO\r\n"
    assert (workspace / "added.txt").read_text(encoding="utf-8") == "new\n"
    assert "--check" not in result.command


@pytest.mark.asyncio
async def test_apply_patch_uses_git_directory_from_nested_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    nested = repository / "nested"
    nested.mkdir(parents=True)
    target = nested / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "add", "nested/file.txt"], cwd=repository, check=True
    )
    _, config, store, session_id = _executor_session(nested, tmp_path / "state")

    result = await apply_patch_execute(
        config,
        store,
        """*** Begin Patch
*** Update File: file.txt
@@
-old
+new
*** End Patch
""",
        ".",
        session_id,
    )

    assert result.applied is True
    assert "--directory nested" in result.command
    assert target.read_text(encoding="utf-8") == "new\n"


@pytest.mark.asyncio
async def test_apply_patch_failed_preflight_does_not_modify_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "sample.txt"
    target.write_text("actual\n", encoding="utf-8")
    _, config, store, session_id = _executor_session(
        workspace, tmp_path / "state"
    )
    patch = """*** Begin Patch
*** Update File: sample.txt
@@
-missing
+replacement
*** End Patch
"""

    with pytest.raises(ValueError, match="does not match"):
        await apply_patch_execute(config, store, patch, ".", session_id)

    assert target.read_text(encoding="utf-8") == "actual\n"


@pytest.mark.asyncio
async def test_apply_patch_rejects_cwd_outside_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _, config, store, session_id = _executor_session(
        workspace, tmp_path / "state"
    )

    with pytest.raises(ValueError, match="escapes (session workdir|workspace)"):
        await apply_patch_execute(
            config,
            store,
            "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n",
            str(outside),
            session_id,
        )


@pytest.mark.asyncio
async def test_apply_patch_uses_frozen_executor_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    settings, config, store, session_id = _executor_session(
        workspace, tmp_path / "state"
    )
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    settings.workspace_root = other_workspace
    settings.git_bin = "definitely-not-the-configured-git"
    settings.max_file_write_bytes = 1

    result = await apply_patch_execute(
        config,
        store,
        """*** Begin Patch
*** Update File: sample.txt
@@
-old
+new
*** End Patch
""",
        ".",
        session_id,
    )

    assert result.applied is True
    assert target.read_text(encoding="utf-8") == "new\n"


def test_apply_patch_uses_native_git_directory_argument() -> None:
    args = _git_apply_args(
        "git", Path("patch file.diff"), "nested dir", check=True
    )
    assert args == [
        "git",
        "apply",
        "--check",
        "--directory",
        "nested dir",
        "patch file.diff",
    ]
