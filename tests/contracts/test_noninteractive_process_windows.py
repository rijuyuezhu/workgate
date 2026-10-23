"""Console-less execution flags for non-interactive helper commands."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.platform.process import noninteractive_creationflags


def test_noninteractive_flag_is_windows_only():
    assert noninteractive_creationflags(system="Windows") == 0x08000000
    assert noninteractive_creationflags(system="Linux") == 0
    assert noninteractive_creationflags(system="Darwin") == 0


def test_git_orientation_passes_windows_no_console_flag(monkeypatch, tmp_path):
    import workgate.executor.session_orientation as orientation

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="/test/project\n")

    monkeypatch.setattr(orientation.subprocess, "run", fake_run)
    monkeypatch.setattr(
        orientation, "noninteractive_creationflags", lambda: 0x08000000
    )
    config = resolve_executor_config(
        Settings(default_workdir=tmp_path, state_dir=tmp_path / "state")
    )
    assert orientation._git_output(config, ["rev-parse"], Path(tmp_path))
    assert calls[0]["creationflags"] == 0x08000000
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert calls[0]["capture_output"] is True


def test_environment_probe_passes_no_console_flag(monkeypatch):
    import workgate.executor.environment as environment

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="git 2.0", stderr="")

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    monkeypatch.setattr(
        environment, "noninteractive_creationflags", lambda: 0x08000000
    )
    probe = environment._CommandProbe("git", ("git",), "configured")
    assert environment._probe_command(probe).available
    assert calls[0]["creationflags"] == 0x08000000
    assert calls[0]["stdin"] is subprocess.DEVNULL


def test_apply_patch_uses_no_console_flag(monkeypatch, tmp_path):
    import workgate.executor.patch as patch

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(patch.subprocess, "run", fake_run)
    monkeypatch.setattr(
        patch, "noninteractive_creationflags", lambda: 0x08000000
    )
    result = patch._run_git_apply(["git", "apply", "--check"], tmp_path)
    assert result["ok"] is True
    assert calls[0]["creationflags"] == 0x08000000
    assert calls[0]["stdin"] is subprocess.DEVNULL
