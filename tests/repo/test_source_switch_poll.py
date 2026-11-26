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


@pytest.mark.parametrize("outcome", ["confirmed", "timeout", "interrupted"])
def test_automatic_linux_switch_verifies_poll_before_commit(
    switcher, tmp_path, monkeypatch, outcome
):
    root = tmp_path / "managed"
    root.mkdir()
    old = root / "versions/old/.venv/bin/python"
    new = root / "versions/new/.venv/bin/python"
    config = tmp_path / "executor.yaml"
    config.write_text("mode: http\n", encoding="utf-8")
    unit = tmp_path / "workgate-executor.service"
    profile = tmp_path / "executor/profile.json"
    profile.parent.mkdir()
    profile.write_bytes(b"unchanged profile")
    active = [old]
    installed = []
    stable = [old]

    def binding(python, saved_config, *, current):
        assert saved_config == config and current
        if python != active[0]:
            raise RuntimeError("runtime is not active")
        return unit, profile

    def run(python, *_args, cwd):
        active[0] = Path(python)
        installed.append(active[0])
        return ""

    waits = 0

    def wait(python, paired_profile, started_at):
        nonlocal waits
        waits += 1
        assert (python, paired_profile) == (new, profile)
        assert active[0] == new and stable[0] == old
        assert started_at > 0
        if outcome == "timeout":
            raise RuntimeError("candidate poll timeout")
        if outcome == "interrupted" and waits == 1:
            raise KeyboardInterrupt

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
    if outcome == "confirmed":
        switcher._switch_executor(*args, require_controller_poll=True)
        assert stable == active == [new]
        assert installed == [new]
    elif outcome == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            switcher._switch_executor(*args, require_controller_poll=True)
        assert stable == [old] and active == [new]
        assert (root / ".executor-cutover.json").is_file()
        monkeypatch.setattr(switcher.sys, "platform", "linux")
        for key in tuple(switcher.os.environ):
            if key.startswith("WORKGATE_"):
                monkeypatch.delenv(key)
        monkeypatch.setattr(switcher, "_old_python", lambda _root: stable[0])
        monkeypatch.setattr(switcher, "_staged_python", lambda *_: new)
        switcher.switch(
            root=root, tag="v5.0.0a99", role="executor", executor_config=config
        )
        assert stable == active == [new]
        assert installed == [new]
    else:
        with pytest.raises(RuntimeError, match="previous service restored"):
            switcher._switch_executor(*args, require_controller_poll=True)
        assert stable == active == [old]
        assert installed == [new, old]
    assert profile.read_bytes() == b"unchanged profile"
    assert not (root / ".executor-cutover.json").exists()


@pytest.mark.parametrize(
    "case",
    [
        "old-active",
        "published",
        "published-unconfirmed",
        "unconfirmed",
        "changed-profile",
        "changed-config",
        "wrong-target",
        "poll-timeout",
    ],
)
def test_recover_trial_respects_binding_and_identity(switcher, tmp_path, case):
    root = tmp_path / "managed"
    root.mkdir()
    previous = root / "versions/old/.venv/bin/python"
    candidate = root / "versions/new/.venv/bin/python"
    config = tmp_path / "executor.yaml"
    profile = tmp_path / "profile.json"
    config.write_text("config", encoding="utf-8")
    profile.write_text("identity", encoding="utf-8")
    current = candidate if case.startswith("published") else previous
    active = previous if case == "old-active" else candidate
    switcher.begin_trial(root, previous, candidate, config, profile, 1.0)
    record = root / ".executor-cutover.json"
    if case == "changed-profile":
        profile.write_text("changed", encoding="utf-8")
    if case == "changed-config":
        config.write_text("changed", encoding="utf-8")
    requested = root / "another" if case == "wrong-target" else candidate
    writes = []
    polls = []

    def binding(python):
        if case == "unconfirmed" or python != active:
            raise RuntimeError("unconfirmed")
        return profile

    def wait(python, paired_profile, started):
        polls.append((python, paired_profile, started))
        if case != "published":
            raise RuntimeError("poll timeout")

    def publish(root, python):
        writes.append(python)
        return root / "bin/workgate"

    def recover():
        return switcher.recover_trial(
            root,
            current,
            requested,
            config,
            binding=binding,
            wait_for_poll=wait,
            publish=publish,
        )

    if case == "published":
        assert recover()
    else:
        with pytest.raises(RuntimeError):
            recover()
    assert record.exists() == (case not in {"published", "old-active"})
    assert writes == []
    assert polls == (
        [(candidate, profile, 1.0)]
        if case in {"poll-timeout", "published", "published-unconfirmed"}
        else []
    )


def test_executor_switch_lock_excludes_another_switch(switcher, tmp_path):
    with (
        switcher.executor_switch_lock(tmp_path),
        pytest.raises((RuntimeError, OSError)),
        switcher.executor_switch_lock(tmp_path),
    ):
        pytest.fail("second switch acquired active lock")
    with switcher.executor_switch_lock(tmp_path):
        pass
