"""Candidate delivery evidence and automatic Linux source-switch rollback."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from workgate.executor import connection_status
from workgate.executor.connection_status import ExecutorConnectionStatusStore
from workgate.persistence import FileStateStore


@pytest.fixture
def switcher(monkeypatch):
    source = Path(__file__).resolve().parents[2] / "deploy/source"
    monkeypatch.syspath_prepend(str(source))
    spec = importlib.util.spec_from_file_location(
        "wg_source_poll_switch", source / "switch.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_poll_requires_fresh_matching_delivery_report(
    switcher, tmp_path, monkeypatch
):
    store = ExecutorConnectionStatusStore(
        FileStateStore(lambda: tmp_path / "state")
    )
    profile = store.path.with_name("profile.json")
    candidate = tmp_path / "candidate/bin/python"
    old = tmp_path / "old/bin/python"
    clock = [1760000000.0]
    monkeypatch.setattr(connection_status.time, "time", lambda: clock[0])
    monkeypatch.setattr(sys, "executable", str(old))
    store.record_poll()
    start = clock[0] + 1
    clock[0] += 2
    assert not switcher._controller_poll_confirmed(candidate, profile, start)
    monkeypatch.setattr(sys, "executable", str(candidate))
    store.record("connected")
    assert not switcher._controller_poll_confirmed(candidate, profile, start)
    store.record_poll()
    assert switcher._controller_poll_confirmed(candidate, profile, start)
    assert switcher._controller_poll_confirmed(
        candidate.with_name("pythonw.exe"), profile, start
    )
    assert not switcher._controller_poll_confirmed(old, profile, start)
    assert not switcher._controller_poll_confirmed(
        candidate, profile, clock[0] + 1
    )
    clock[0] += 91
    assert not switcher._controller_poll_confirmed(candidate, profile, start)
    store.record("reconnecting")
    assert not switcher._controller_poll_confirmed(candidate, profile, start)
    store.path.write_bytes(b"{" * 1025)
    assert not switcher._controller_poll_confirmed(candidate, profile, start)
    store.path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
    assert not switcher._controller_poll_confirmed(candidate, profile, start)


def test_candidate_poll_timeout_is_bounded(switcher, tmp_path, monkeypatch):
    ticks = iter([0.0, 91.0])
    monkeypatch.setattr(switcher.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        switcher, "_controller_poll_confirmed", lambda *_: False
    )
    with pytest.raises(RuntimeError, match="did not complete a Control poll"):
        switcher._wait_for_controller_poll(
            tmp_path / "python", tmp_path / "profile.json", 0.0
        )


@pytest.mark.parametrize("confirmed", [True, False])
def test_automatic_linux_switch_verifies_poll_before_commit(
    switcher, tmp_path, monkeypatch, confirmed
):
    root = tmp_path / "managed"
    old = root / "versions/old/.venv/bin/python"
    new = root / "versions/new/.venv/bin/python"
    config = tmp_path / "executor.yaml"
    unit = tmp_path / "workgate-executor.service"
    profile = tmp_path / "executor/profile.json"
    profile.parent.mkdir()
    profile.write_bytes(b"unchanged profile")
    active = [old]
    installed = []
    stable = [old]

    def binding(python, saved_config, *, current):
        assert saved_config == config and current and python == active[0]
        return unit, profile

    def run(python, *_args, cwd):
        active[0] = Path(python)
        installed.append(active[0])
        return ""

    def wait(python, paired_profile, started_at):
        assert (python, paired_profile) == (new, profile)
        assert active[0] == new and stable[0] == old
        assert started_at > 0
        if not confirmed:
            raise RuntimeError("candidate poll timeout")

    monkeypatch.setattr(switcher, "_executor_binding", binding)
    monkeypatch.setattr(
        switcher,
        "_executor_status",
        lambda *_: {
            "State": "running",
            "Service": str(unit),
            "Log": str(profile.parent / "service/executor.log"),
            "Backend": "systemd",
            "Runtime owner": "source",
        },
    )
    monkeypatch.setattr(switcher, "_run", run)
    monkeypatch.setattr(switcher, "_wait_for_controller_poll", wait)
    monkeypatch.setattr(
        switcher,
        "_write_launcher",
        lambda _root, python: stable.__setitem__(0, python),
    )
    args = (root, old, new, "v5.0.0a99", config)
    if confirmed:
        switcher._switch_executor(*args, require_controller_poll=True)
        assert stable == active == [new]
        assert installed == [new]
    else:
        with pytest.raises(RuntimeError, match="previous service restored"):
            switcher._switch_executor(*args, require_controller_poll=True)
        assert stable == active == [old]
        assert installed == [new, old]
    assert profile.read_bytes() == b"unchanged profile"
