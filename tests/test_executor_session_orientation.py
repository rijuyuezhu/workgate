import subprocess
from pathlib import Path

from tests.helpers import build_tool_session_store
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.session_orientation import (
    _git_info,
    _git_output,
    _instruction_files,
    _relative_display,
    change_session_cwd,
    session_output,
)


def _config(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
    )
    return settings, resolve_executor_config(settings)


def test_session_orientation_discovers_workspace_instructions(
    tmp_path: Path,
) -> None:
    _settings, config = _config(tmp_path)
    nested = config.default_workdir / "project" / "src"
    nested.mkdir(parents=True)
    (config.default_workdir / "AGENTS.md").write_text(
        "root\n", encoding="utf-8"
    )
    (nested / "CLAUDE.md").write_text("nested\n", encoding="utf-8")

    assert [Path(path) for path in _instruction_files(config, nested)] == [
        Path("project") / "src" / "CLAUDE.md",
        Path("AGENTS.md"),
    ]
    assert Path(_relative_display(nested, config.default_workdir)) == (
        Path("project") / "src"
    )
    outside = tmp_path / "outside"
    assert _relative_display(outside, config.default_workdir) == str(
        outside.resolve()
    )
    assert _instruction_files(config, outside) == []


def test_git_orientation_handles_success_and_command_failure(
    tmp_path: Path,
) -> None:
    _settings, config = _config(tmp_path)
    subprocess.run(
        [config.git_bin, "init", "-q", str(config.default_workdir)],
        check=True,
        capture_output=True,
        text=True,
    )
    (config.default_workdir / "dirty.txt").write_text(
        "dirty\n", encoding="utf-8"
    )

    info = _git_info(config, config.default_workdir)

    assert info.is_repo is True
    assert Path(info.root or "").resolve() == config.default_workdir
    assert info.dirty is True
    assert (
        _git_output(
            config, ["not-a-real-git-subcommand"], config.default_workdir
        )
        is None
    )


def test_session_output_and_change_cwd_use_executor_authority(
    tmp_path: Path,
) -> None:
    settings, config = _config(tmp_path)
    next_dir = config.default_workdir / "next"
    next_dir.mkdir()
    store = build_tool_session_store(settings)
    session = store.create_session(
        session_id="sess_0000000000000000000001",
        workdir=config.default_workdir,
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
    assert initial.default_workdir == str(config.default_workdir)
    assert initial.label == "orientation"
    assert changed.session_id == session.session_id
    assert Path(changed.workdir) == next_dir
    assert changed.default_workdir == str(config.default_workdir)
