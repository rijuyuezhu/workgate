from __future__ import annotations

import subprocess
from pathlib import Path

from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.session_orientation import (
    _git_info,
    _git_output,
    _instruction_files,
    _relative_display,
    change_session_cwd,
    session_output,
)
from workgate.persistence import FileStateStore
from workgate.tool_session.store import ToolSessionStore


def _config(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    settings = Settings(
        workspace_root=workspace,
        state_dir=tmp_path / "state",
        remote_enabled=False,
        agent_bridge_enabled=False,
    )
    return settings, resolve_executor_config(settings)


def test_session_orientation_discovers_workspace_instructions(
    tmp_path: Path,
) -> None:
    _settings, config = _config(tmp_path)
    nested = config.workspace_root / "project" / "src"
    nested.mkdir(parents=True)
    (config.workspace_root / "AGENTS.md").write_text("root\n", encoding="utf-8")
    (nested / "CLAUDE.md").write_text("nested\n", encoding="utf-8")

    assert _instruction_files(config, nested) == [
        "project/src/CLAUDE.md",
        "AGENTS.md",
    ]
    assert _relative_display(nested, config.workspace_root) == "project/src"
    outside = tmp_path / "outside"
    assert _relative_display(outside, config.workspace_root) == str(
        outside.resolve()
    )
    assert _instruction_files(config, outside) == []


def test_git_orientation_handles_success_and_command_failure(
    tmp_path: Path,
) -> None:
    _settings, config = _config(tmp_path)
    subprocess.run(
        [config.git_bin, "init", "-q", str(config.workspace_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    (config.workspace_root / "dirty.txt").write_text(
        "dirty\n", encoding="utf-8"
    )

    info = _git_info(config, config.workspace_root)

    assert info.is_repo is True
    assert Path(info.root or "").resolve() == config.workspace_root
    assert info.dirty is True
    assert (
        _git_output(
            config, ["not-a-real-git-subcommand"], config.workspace_root
        )
        is None
    )


def test_session_output_and_change_cwd_use_executor_authority(
    tmp_path: Path,
) -> None:
    settings, config = _config(tmp_path)
    next_dir = config.workspace_root / "next"
    next_dir.mkdir()
    store = ToolSessionStore(
        FileStateStore(lambda: settings.state_dir),
        settings_provider=lambda: settings,
    )
    session = store.create_session(
        session_id="sess_0000000000000000000001",
        workdir=config.workspace_root,
        label="orientation",
    )

    initial = session_output(config, session)
    changed = change_session_cwd(
        config,
        store,
        session.session_id,
        "next",
    )

    assert initial.session_id == session.session_id
    assert initial.workspace_root == str(config.workspace_root)
    assert initial.label == "orientation"
    assert changed.session_id == session.session_id
    assert Path(changed.workdir) == next_dir
    assert changed.workspace_root == str(config.workspace_root)
