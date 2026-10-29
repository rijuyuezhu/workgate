"""macOS Executor native launchd ownership checks and rollback, no real service changes."""

import importlib.util
import os
import plistlib
from pathlib import Path

import pytest


@pytest.fixture
def switcher(monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[2] / "deploy/source"
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location(
        "wg_source_switch_macos", root / "switch_macos.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    for name in tuple(os.environ):
        if name.startswith("WORKGATE_"):
            monkeypatch.delenv(name)
    return module


@pytest.fixture
def installation(tmp_path: Path, switcher, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "managed source"
    old = root / "versions" / ("a" * 40) / ".venv/bin/python"
    new = root / "versions" / ("b" * 40) / ".venv/bin/python"
    for python in (old, new):
        python.parent.mkdir(parents=True)
        python.write_bytes(b"mock")
    config = tmp_path / "executor config.yaml"
    config.write_text("workdir: original\n")
    directory = tmp_path / "state/executor/service"
    directory.mkdir(parents=True)
    profile = directory.parent / "profile.json"
    profile.write_bytes(b"original paired profile")
    launcher = directory / "launcher.py"
    launcher.write_text("original executor launcher")
    log = directory / "executor.log"
    log.write_text("")
    plist = tmp_path / "Library/LaunchAgents/com.workgate.executor.plist"
    plist.parent.mkdir(parents=True)
    stable = root / "bin/workgate"
    stable.parent.mkdir()
    stable.write_text("old stable cli")
    state = {
        "active": old,
        "installs": [],
        "fail_new": False,
        "fail_old": False,
        "dirty_identity": False,
        "loaded": True,
    }
    monkeypatch.setattr(switcher, "_old_python", lambda root: old)
    monkeypatch.setattr(switcher, "_staged_python", lambda root, tag: new)

    def native_plist(python: Path):
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.workgate.executor",
                    "ProgramArguments": [str(python), str(launcher)],
                }
            )
        )

    native_plist(old)

    def status(python, saved_config):
        assert saved_config == config
        return {
            "State": "running",
            "Backend": "launchd",
            "Service": str(plist),
            "Log": str(log),
            "Runtime owner": "source",
            "Runtime": "current" if python == state["active"] else "stale",
        }

    def run(*args, cwd=None):
        assert args == (
            "launchctl",
            "print",
            f"gui/{os.getuid()}/com.workgate.executor",
        )
        if not state["loaded"]:
            return "path = /foreign/agent.plist\nstate = running\nprogram = /usr/bin/python"
        python = state["active"]
        return f"path = {plist}\nstate = running\nprogram = {python}\n"

    def install(python, saved_config):
        assert saved_config == config
        state["installs"].append(python)
        if python == new and state["fail_new"]:
            raise RuntimeError("new runtime failed")
        if python == old and state["fail_old"]:
            raise RuntimeError("old rollback failed")
        state["active"] = python
        native_plist(python)
        if state["dirty_identity"] and python == new:
            profile.write_bytes(b"identity changed")

    monkeypatch.setattr(switcher, "_status", status)
    monkeypatch.setattr(switcher, "_run", run)
    monkeypatch.setattr(switcher, "_install", install)
    monkeypatch.setattr(
        switcher,
        "_write_launcher",
        lambda root, python: stable.write_text(str(python)),
    )
    return root, config, old, new, plist, profile, stable, state


def test_macos_switch_preserves_identity_and_changes_stable_launcher(
    switcher,
    installation,
):
    root, config, old, new, _plist, profile, stable, state = installation
    old_identity = profile.read_bytes()
    switcher.switch(root=root, tag="v5.0.0a99", config=config)
    assert state["active"] == new
    assert state["installs"] == [new]
    assert profile.read_bytes() == old_identity
    assert stable.read_text() == str(new)


def test_macos_failed_install_restores_old_runtime(switcher, installation):
    root, config, old, new, _plist, profile, stable, state = installation
    state["fail_new"] = True
    with pytest.raises(RuntimeError, match="old service restored"):
        switcher.switch(root=root, tag="v5.0.0a99", config=config)
    assert state["installs"] == [new, old]
    assert state["active"] == old
    assert stable.read_text() == str(old)
    assert profile.read_bytes() == b"original paired profile"


def test_macos_recovery_failure_reports_both(switcher, installation):
    root, config, _old, _new, _plist, _profile, _stable, state = installation
    state["fail_new"] = True
    state["fail_old"] = True
    with pytest.raises(RuntimeError, match="recovery also failed"):
        switcher.switch(root=root, tag="v5.0.0a99", config=config)


def test_macos_identity_change_refuses_success(switcher, installation):
    root, config, old, new, _plist, _profile, stable, state = installation
    state["dirty_identity"] = True
    with pytest.raises(RuntimeError, match="recovery also failed"):
        switcher.switch(root=root, tag="v5.0.0a99", config=config)
    assert state["installs"] == [new, old]
    assert stable.read_text() == "old stable cli"


@pytest.mark.parametrize(
    "fault",
    [
        "foreign-launchd",
        "foreign-plist-program",
        "symlink-plist",
        "missing-profile",
        "symlink-launcher",
        "dirty-env",
        "invalid-tag",
        "wrong-platform",
    ],
)
def test_macos_rejects_untrusted_preflight(
    switcher, installation, tmp_path, monkeypatch, fault
):
    root, config, _old, _new, plist, profile, stable, state = installation
    if fault == "foreign-launchd":
        state["loaded"] = False
    elif fault == "foreign-plist-program":
        payload = plistlib.loads(plist.read_bytes())
        payload["ProgramArguments"][0] = "/untrusted/python"
        plist.write_bytes(plistlib.dumps(payload))
    elif fault == "symlink-plist":
        plist.unlink()
        other = tmp_path / "untrusted.plist"
        other.write_text("bad")
        plist.symlink_to(other)
    elif fault == "missing-profile":
        profile.unlink()
    elif fault == "symlink-launcher":
        p = profile.parent / "service/launcher.py"
        p.unlink()
        other = tmp_path / "untrusted.py"
        other.write_text("bad")
        p.symlink_to(other)
    elif fault == "dirty-env":
        monkeypatch.setenv("WORKGATE_LOG_LEVEL", "debug")
    elif fault == "wrong-platform":
        monkeypatch.setattr(switcher.sys, "platform", "linux")

    with pytest.raises((RuntimeError, ValueError)):
        switcher.switch(
            root=root,
            tag="../evil" if fault == "invalid-tag" else "v5.0.0a99",
            config=config,
        )
    assert state["installs"] == []
    assert stable.read_text() == "old stable cli"


def test_binding_checks_live_launchctl_and_plist(switcher, installation):
    _root, config, old, _new, plist, profile, _stable, state = installation
    assert switcher._binding(old, config, current=True) == profile
    state["loaded"] = False
    with pytest.raises(RuntimeError, match="loaded LaunchAgent"):
        switcher._binding(old, config, current=True)


def test_macos_bootstrap_waits_for_running_state(
    switcher, installation, monkeypatch: pytest.MonkeyPatch
):
    root, config, old, new, _plist, _profile, stable, state = installation
    original = switcher._binding
    calls = []

    def delayed(python, config, *, current):
        if python == new and len(calls) < 3:
            calls.append("waiting")
            raise RuntimeError("launchd still starting")
        return original(python, config, current=current)

    monkeypatch.setattr(switcher, "_binding", delayed)
    monkeypatch.setattr(switcher.time, "sleep", lambda _delay: None)
    switcher.switch(root=root, tag="v5.0.0a99", config=config)
    assert calls == ["waiting"] * 3
    assert stable.read_text() == str(new)
