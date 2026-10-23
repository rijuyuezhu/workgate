"""Console-less execution flags for non-interactive helper commands."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows only")
def test_pythonw_child_has_no_console_and_keeps_output(tmp_path):
    """Exercise a native console-subsystem subprocess from pythonw.exe."""
    import json

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    assert pythonw.is_file()
    output = tmp_path / "result.json"
    script = tmp_path / "launch.py"
    script.write_text(
        "import ctypes, json, pathlib, subprocess, sys\n"
        "result = subprocess.run([sys.argv[2], '-c', "
        "'import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())'], "
        "stdin=subprocess.DEVNULL, capture_output=True, text=True, "
        "creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'rc': result.returncode, 'out': result.stdout.strip()"
        "}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [str(pythonw), str(script), str(output), sys.executable],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=noninteractive_creationflags(system="Windows"),
        timeout=20,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result == {"rc": 0, "out": "0"}
