import argparse
import contextlib
import os
import plistlib
import shlex
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from workgate.executor import bounded_runner


def _install_fake_kqueue_capabilities(monkeypatch, queue_factory):
    constants = {
        "KQ_FILTER_PROC": -5,
        "KQ_EV_ADD": 1,
        "KQ_EV_ENABLE": 2,
        "KQ_NOTE_EXIT": 4,
        "KQ_NOTE_FORK": 8,
        "KQ_NOTE_TRACK": 16,
        "KQ_NOTE_CHILD": 32,
        "KQ_NOTE_TRACKERR": 64,
    }
    monkeypatch.setattr(
        bounded_runner.select, "kqueue", queue_factory, raising=False
    )
    monkeypatch.setattr(
        bounded_runner.select,
        "kevent",
        lambda ident, **kwargs: SimpleNamespace(ident=ident, **kwargs),
        raising=False,
    )
    for name, value in constants.items():
        monkeypatch.setattr(bounded_runner.select, name, value, raising=False)
    return SimpleNamespace(**constants)


def _install_fake_launchd_coalitions(monkeypatch):
    """Give in-process launchd parent/child tests distinct coalition identities."""
    parent_thread = threading.current_thread()
    monkeypatch.setattr(
        bounded_runner,
        "_macos_process_coalition_ids",
        lambda _pid: (
            (101, 11)
            if threading.current_thread() is parent_thread
            else (202, 22)
        ),
    )
    monkeypatch.setattr(
        bounded_runner, "_cleanup_macos_coalition", lambda _ids: True
    )


def test_shell_command_args_support_native_windows_shells():
    assert bounded_runner._shell_command_args("pwsh.exe", "echo ok") == [
        "pwsh.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "echo ok",
    ]
    assert bounded_runner._shell_command_args("cmd.exe", "echo ok") == [
        "cmd.exe",
        "/D",
        "/S",
        "/C",
        "echo ok",
    ]


def test_handled_signals_omit_platform_missing_sighup(monkeypatch):
    monkeypatch.delattr(bounded_runner.signal, "SIGHUP", raising=False)

    assert bounded_runner._handled_signals() == (
        signal.SIGINT,
        signal.SIGTERM,
    )


def test_enable_child_subreaper_is_disabled_off_linux(monkeypatch):
    monkeypatch.setattr(bounded_runner.sys, "platform", "win32")

    assert bounded_runner._enable_child_subreaper() is False


def test_enable_child_subreaper_handles_missing_prctl(monkeypatch):
    class MissingPrctl:
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(
        bounded_runner.ctypes, "CDLL", lambda *a, **k: MissingPrctl()
    )

    assert bounded_runner._enable_child_subreaper() is False


def test_kqueue_descendant_tracking_requires_every_capability(monkeypatch):
    monkeypatch.setattr(bounded_runner.sys, "platform", "freebsd14")
    monkeypatch.setattr(bounded_runner.os, "fork", lambda: 42, raising=False)
    monkeypatch.setattr(
        bounded_runner.os, "waitpid", lambda _pid, _flags: (0, 0), raising=False
    )
    _install_fake_kqueue_capabilities(monkeypatch, lambda: None)
    assert bounded_runner._kqueue_descendant_tracking_available() is True

    monkeypatch.delattr(bounded_runner.select, "KQ_NOTE_TRACKERR")
    assert bounded_runner._kqueue_descendant_tracking_available() is False


def test_kqueue_tracker_registers_and_tracks_children(monkeypatch):
    class FakeQueue:
        def __init__(self):
            self.registrations = []
            self.batches = []
            self.closed = False

        def control(self, changes, _max_events, _timeout):
            if changes is not None:
                self.registrations.extend(changes)
                return []
            return self.batches.pop(0) if self.batches else []

        def close(self):
            self.closed = True

    queue = FakeQueue()
    flags = _install_fake_kqueue_capabilities(monkeypatch, lambda: queue)
    tracker = bounded_runner._KqueueDescendantTracker.create(42)

    assert len(queue.registrations) == 1
    registration = queue.registrations[0]
    assert registration.ident == 42
    assert registration.filter == flags.KQ_FILTER_PROC
    assert registration.fflags == (
        flags.KQ_NOTE_EXIT | flags.KQ_NOTE_FORK | flags.KQ_NOTE_TRACK
    )

    queue.batches.extend(
        [
            [SimpleNamespace(ident=43, fflags=flags.KQ_NOTE_CHILD)],
            [],
        ]
    )
    tracker.poll()
    assert tracker.live_pids == {42, 43}

    queue.batches.extend(
        [[SimpleNamespace(ident=43, fflags=flags.KQ_NOTE_EXIT)], []]
    )
    tracker.poll()
    assert tracker.live_pids == {42}
    tracker.close()
    assert queue.closed is True


def test_kqueue_tracker_marks_track_errors_and_signals_known_pids(
    monkeypatch,
):
    class FakeQueue:
        def __init__(self):
            self.batches = []

        def control(self, changes, _max_events, _timeout):
            if changes is not None:
                return []
            return self.batches.pop(0) if self.batches else []

        def close(self):
            pass

    queue = FakeQueue()
    flags = _install_fake_kqueue_capabilities(monkeypatch, lambda: queue)
    tracker = bounded_runner._KqueueDescendantTracker.create(42)
    queue.batches.extend(
        [
            [
                SimpleNamespace(ident=43, fflags=flags.KQ_NOTE_CHILD),
                SimpleNamespace(ident=42, fflags=flags.KQ_NOTE_TRACKERR),
            ],
            [],
        ]
    )
    killed = []
    monkeypatch.setattr(
        bounded_runner.os, "kill", lambda pid, sig: killed.append((pid, sig))
    )

    tracker.poll()
    assert tracker.failed is True
    assert tracker.cleanup() is False
    assert (43, signal.SIGTERM) in killed
    assert (42, signal.SIGTERM) in killed
    assert (43, bounded_runner._force_kill_signal()) in killed


def test_select_capability_rejects_missing_value(monkeypatch):
    monkeypatch.delattr(
        bounded_runner.select, "missing_capability", raising=False
    )

    with pytest.raises(RuntimeError, match="missing_capability"):
        bounded_runner._select_capability("missing_capability")


def test_kqueue_tracker_closes_queue_when_registration_fails(monkeypatch):
    class FakeQueue:
        closed = False

        def control(self, *_args):
            raise OSError("registration failed")

        def close(self):
            self.closed = True

    queue = FakeQueue()
    _install_fake_kqueue_capabilities(monkeypatch, lambda: queue)

    with pytest.raises(OSError, match="registration failed"):
        bounded_runner._KqueueDescendantTracker.create(42)

    assert queue.closed is True


def test_kqueue_tracker_handles_poll_and_signal_errors(monkeypatch):
    class FakeQueue:
        def control(self, changes, _max_events, _timeout):
            if changes is not None:
                return []
            raise OSError("queue failed")

        def close(self):
            pass

    queue = FakeQueue()
    _install_fake_kqueue_capabilities(monkeypatch, lambda: queue)
    tracker = bounded_runner._KqueueDescendantTracker.create(42)
    tracker.poll()
    assert tracker.failed is True

    tracker.failed = False
    tracker.live_pids = {42, 43}

    def fake_kill(pid, _sig):
        if pid == 43:
            raise ProcessLookupError
        raise PermissionError

    monkeypatch.setattr(bounded_runner.os, "kill", fake_kill)
    tracker.signal_all(signal.SIGTERM)
    assert tracker.live_pids == {42}
    assert tracker.failed is True


def test_kqueue_tracker_wait_and_graceful_cleanup(monkeypatch):
    tracker = bounded_runner._KqueueDescendantTracker(
        SimpleNamespace(control=lambda *_args: [], close=lambda: None), 42
    )
    tracker.discard_root()
    assert tracker.live_pids == set()
    assert tracker.cleanup() is True

    tracker.live_pids = {43}
    signals = []
    monkeypatch.setattr(tracker, "poll", lambda: tracker.live_pids.clear())
    monkeypatch.setattr(tracker, "signal_all", lambda sig: signals.append(sig))
    assert tracker.wait_for_empty(1.0) is True

    tracker.live_pids = {43}
    poll_count = 0

    def clear_after_signal():
        nonlocal poll_count
        poll_count += 1
        if poll_count > 1:
            tracker.live_pids.clear()

    monkeypatch.setattr(tracker, "poll", clear_after_signal)
    assert tracker.cleanup() is True
    assert signals == [signal.SIGTERM]


def test_cleanup_kqueue_processes_rechecks_group(monkeypatch):
    calls = []
    tracker = bounded_runner._KqueueDescendantTracker(
        SimpleNamespace(control=lambda *_args: [], close=lambda: None), 42
    )
    monkeypatch.setattr(
        tracker, "discard_root", lambda: calls.append("discard")
    )
    monkeypatch.setattr(tracker, "cleanup", lambda: True)
    monkeypatch.setattr(
        bounded_runner, "_cleanup_process_group", lambda _pgid: False
    )
    monkeypatch.setattr(
        bounded_runner,
        "_wait_for_process_group_exit",
        lambda pgid, timeout: calls.append((pgid, timeout)) or True,
    )

    assert (
        bounded_runner._cleanup_kqueue_command_processes(
            42, tracker, root_exited=True
        )
        is True
    )
    assert calls == [
        "discard",
        (42, bounded_runner.POLL_INTERVAL_S * 5),
    ]


def test_direct_children_parses_procfs_pids(tmp_path, monkeypatch):
    children_path = tmp_path / "7" / "task" / "7" / "children"
    children_path.parent.mkdir(parents=True)
    children_path.write_text("12 34 56\n", encoding="ascii")
    monkeypatch.setattr(bounded_runner, "PROCFS_ROOT", tmp_path)

    assert bounded_runner._direct_children(7) == [12, 34, 56]


@pytest.mark.parametrize("contents", [None, b"\xff"])
def test_direct_children_reports_unavailable_procfs(
    tmp_path, monkeypatch, contents
):
    process_dir = tmp_path / "7"
    process_dir.mkdir()
    if contents is not None:
        children_path = process_dir / "task" / "7" / "children"
        children_path.parent.mkdir(parents=True)
        children_path.write_bytes(contents)
    monkeypatch.setattr(bounded_runner, "PROCFS_ROOT", tmp_path)

    assert bounded_runner._direct_children(7) is None


def test_direct_children_treats_vanished_process_as_empty(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bounded_runner, "PROCFS_ROOT", tmp_path)

    assert bounded_runner._direct_children(7) == []


def test_direct_children_rejects_malformed_procfs(tmp_path, monkeypatch):
    children_path = tmp_path / "7" / "task" / "7" / "children"
    children_path.parent.mkdir(parents=True)
    children_path.write_text("12 unexpected 56\n", encoding="ascii")
    monkeypatch.setattr(bounded_runner, "PROCFS_ROOT", tmp_path)

    assert bounded_runner._direct_children(7) is None


def test_wait_for_no_descendants_reaps_until_empty(monkeypatch):
    descendants = iter([[3], []])
    reaped: list[bool] = []
    clock = iter([0.0, 0.1, 0.2])
    monkeypatch.setattr(bounded_runner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        bounded_runner, "_descendants", lambda _pid: next(descendants)
    )
    monkeypatch.setattr(
        bounded_runner, "_reap_children", lambda: reaped.append(True)
    )
    monkeypatch.setattr(bounded_runner.time, "sleep", lambda _seconds: None)

    assert bounded_runner._wait_for_no_descendants(1.0) is True
    assert reaped == [True, True]


def test_wait_for_no_descendants_fails_on_unknown_inventory(monkeypatch):
    monkeypatch.setattr(bounded_runner.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(bounded_runner, "_reap_children", lambda: None)
    monkeypatch.setattr(bounded_runner, "_descendants", lambda _pid: None)

    assert bounded_runner._wait_for_no_descendants(1.0) is False


def test_reap_children_stops_when_no_child_is_ready(monkeypatch):
    results = iter([(10, 0), (0, 0)])
    monkeypatch.setattr(bounded_runner.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(
        bounded_runner.os, "waitpid", lambda _pid, _flags: next(results)
    )

    bounded_runner._reap_children()


def test_reap_children_is_noop_without_nonblocking_wait(monkeypatch):
    monkeypatch.delattr(bounded_runner.os, "WNOHANG", raising=False)
    monkeypatch.setattr(
        bounded_runner.os,
        "waitpid",
        lambda *_args: pytest.fail("waitpid must not be called"),
        raising=False,
    )

    bounded_runner._reap_children()


def test_process_tree_helpers_handle_races_and_cycles(monkeypatch):
    def fake_children(pid: int) -> list[int]:
        return {1: [2, 3], 2: [3], 3: [1]}.get(pid, [])

    monkeypatch.setattr(bounded_runner, "_direct_children", fake_children)

    descendants = bounded_runner._descendants(1)
    assert descendants is not None
    assert set(descendants) == {1, 2, 3}


def test_children_file_traversal_tolerates_reaped_descendant(
    tmp_path, monkeypatch
):
    children_path = tmp_path / "1" / "task" / "1" / "children"
    children_path.parent.mkdir(parents=True)
    children_path.write_text("2\n", encoding="ascii")
    monkeypatch.setattr(bounded_runner, "PROCFS_ROOT", tmp_path)

    assert bounded_runner._descendants_from_children_files(1) == [2]


def test_descendants_falls_back_to_procfs_parent_map(monkeypatch):
    monkeypatch.setattr(
        bounded_runner,
        "_direct_children",
        lambda pid: [2] if pid == 1 else None,
    )
    monkeypatch.setattr(
        bounded_runner,
        "_procfs_parent_map",
        lambda: {2: 1, 3: 2, 4: 99},
    )

    assert bounded_runner._descendants(1) == [2, 3]


def test_descendants_rescans_empty_parent_map_for_newly_adopted_child(
    monkeypatch,
):
    parent_maps = iter([{2: 99}, {3: 1}])
    monkeypatch.setattr(bounded_runner, "_direct_children", lambda _pid: None)
    monkeypatch.setattr(
        bounded_runner, "_procfs_parent_map", lambda: next(parent_maps)
    )

    assert bounded_runner._descendants(1) == [3]


def test_descendants_requires_two_authoritative_empty_parent_maps(monkeypatch):
    scans: list[bool] = []
    monkeypatch.setattr(bounded_runner, "_direct_children", lambda _pid: None)

    def empty_parent_map():
        scans.append(True)
        return {2: 99}

    monkeypatch.setattr(bounded_runner, "_procfs_parent_map", empty_parent_map)

    assert bounded_runner._descendants(1) == []
    assert scans == [True, True]


def test_descendants_fails_closed_when_empty_rescan_is_uncertain(monkeypatch):
    parent_maps = iter([{2: 99}, None])
    monkeypatch.setattr(bounded_runner, "_direct_children", lambda _pid: None)
    monkeypatch.setattr(
        bounded_runner, "_procfs_parent_map", lambda: next(parent_maps)
    )

    assert bounded_runner._descendants(1) is None


def test_descendants_propagates_unknown_procfs_inventory(monkeypatch):
    monkeypatch.setattr(bounded_runner, "_direct_children", lambda _pid: None)
    monkeypatch.setattr(bounded_runner, "_procfs_parent_map", lambda: None)

    assert bounded_runner._descendants(1) is None


def test_procfs_parent_map_parses_status_and_ignores_exit_races(
    tmp_path, monkeypatch
):
    proc_root = tmp_path / "proc"
    for pid, ppid in (("10", "1"), ("11", "10")):
        process_dir = proc_root / pid
        process_dir.mkdir(parents=True)
        (process_dir / "status").write_text(
            f"Name:\ttest\nPPid:\t{ppid}\n", encoding="ascii"
        )
    (proc_root / "12").mkdir()
    (proc_root / "not-a-pid").mkdir()
    monkeypatch.setattr(bounded_runner, "PROCFS_ROOT", proc_root)

    assert bounded_runner._procfs_parent_map() == {10: 1, 11: 10}


@pytest.mark.parametrize("status", ["Name:\ttest\n", "PPid:\tnot-a-pid\n"])
def test_procfs_parent_map_rejects_malformed_status(
    tmp_path, monkeypatch, status
):
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "10"
    process_dir.mkdir(parents=True)
    (process_dir / "status").write_text(status, encoding="ascii")
    monkeypatch.setattr(bounded_runner, "PROCFS_ROOT", proc_root)

    assert bounded_runner._procfs_parent_map() is None


def test_procfs_parent_map_rejects_unreadable_root(monkeypatch):
    monkeypatch.setattr(
        bounded_runner,
        "PROCFS_ROOT",
        SimpleNamespace(
            iterdir=lambda: (_ for _ in ()).throw(OSError("denied"))
        ),
    )

    assert bounded_runner._procfs_parent_map() is None


def test_signal_descendants_reports_forbidden_processes(
    monkeypatch,
):
    monkeypatch.setattr(bounded_runner, "_descendants", lambda _pid: [1, 2])

    def fake_kill(pid: int, _sig):
        if pid == 2:
            raise ProcessLookupError
        raise PermissionError

    monkeypatch.setattr(bounded_runner.os, "kill", fake_kill)

    assert bounded_runner._signal_descendants(signal.SIGTERM) is False


def test_signal_descendants_fails_on_unknown_inventory(monkeypatch):
    monkeypatch.setattr(bounded_runner, "_descendants", lambda _pid: None)
    monkeypatch.setattr(
        bounded_runner.os,
        "kill",
        lambda *_args: pytest.fail("unknown descendants must not be signalled"),
    )

    assert bounded_runner._signal_descendants(signal.SIGTERM) is False


def test_cleanup_descendants_escalates_to_sigkill(monkeypatch):
    signals = []
    waits = iter([False, True])
    monkeypatch.setattr(bounded_runner, "_descendants", lambda _pid: [2])
    monkeypatch.setattr(
        bounded_runner,
        "_signal_descendants",
        lambda sig: signals.append(sig) or True,
    )
    monkeypatch.setattr(
        bounded_runner, "_wait_for_no_descendants", lambda _timeout: next(waits)
    )
    monkeypatch.setattr(bounded_runner, "_reap_children", lambda: None)

    assert bounded_runner._cleanup_descendants() is True
    assert signals == [signal.SIGTERM, bounded_runner._force_kill_signal()]


def test_cleanup_descendants_returns_after_graceful_term(monkeypatch):
    signals = []
    monkeypatch.setattr(bounded_runner, "_descendants", lambda _pid: [2])
    monkeypatch.setattr(
        bounded_runner,
        "_signal_descendants",
        lambda sig: signals.append(sig) or True,
    )
    monkeypatch.setattr(
        bounded_runner, "_wait_for_no_descendants", lambda _timeout: True
    )
    monkeypatch.setattr(bounded_runner, "_reap_children", lambda: None)

    assert bounded_runner._cleanup_descendants() is True
    assert signals == [signal.SIGTERM]


def test_cleanup_descendants_fails_on_unknown_inventory(monkeypatch):
    monkeypatch.setattr(bounded_runner, "_descendants", lambda _pid: None)

    assert bounded_runner._cleanup_descendants() is False


def test_force_kill_signal_falls_back_to_sigterm(monkeypatch):
    monkeypatch.delattr(bounded_runner.signal, "SIGKILL", raising=False)

    assert bounded_runner._force_kill_signal() == signal.SIGTERM


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, True),
        (ProcessLookupError(), False),
        (PermissionError(), True),
    ],
)
def test_process_group_alive_handles_platform_results(
    error, expected, monkeypatch
):
    def fake_killpg(_pgid, _signal):
        if error is not None:
            raise error

    monkeypatch.setattr(bounded_runner.os, "killpg", fake_killpg, raising=False)

    assert bounded_runner._process_group_alive(42) is expected


@pytest.mark.parametrize(
    "error", [None, ProcessLookupError(), PermissionError()]
)
def test_signal_process_group_tolerates_exit_races(error, monkeypatch):
    calls = []

    def fake_killpg(pgid, sig):
        calls.append((pgid, sig))
        if error is not None:
            raise error

    monkeypatch.setattr(bounded_runner.os, "killpg", fake_killpg, raising=False)

    bounded_runner._signal_process_group(42, signal.SIGTERM)

    assert calls == [(42, signal.SIGTERM)]


def test_wait_for_process_group_exit_returns_early(monkeypatch):
    clock = iter([0.0, 0.1, 0.2])
    alive = iter([True, False])
    reaped = []
    monkeypatch.setattr(bounded_runner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        bounded_runner, "_process_group_alive", lambda _pgid: next(alive)
    )
    monkeypatch.setattr(
        bounded_runner, "_reap_children", lambda: reaped.append(True)
    )
    monkeypatch.setattr(bounded_runner.time, "sleep", lambda _seconds: None)

    assert bounded_runner._wait_for_process_group_exit(42, 1.0) is True
    assert reaped == [True, True]


def test_wait_for_process_group_exit_reports_timeout(monkeypatch):
    clock = iter([0.0, 0.5, 1.0])
    reaped = []
    monkeypatch.setattr(bounded_runner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        bounded_runner, "_process_group_alive", lambda _pgid: True
    )
    monkeypatch.setattr(
        bounded_runner, "_reap_children", lambda: reaped.append(True)
    )
    monkeypatch.setattr(bounded_runner.time, "sleep", lambda _seconds: None)

    assert bounded_runner._wait_for_process_group_exit(42, 1.0) is False
    assert reaped == [True, True]


def test_cleanup_process_group_returns_when_already_gone(monkeypatch):
    monkeypatch.setattr(
        bounded_runner, "_process_group_alive", lambda _pgid: False
    )
    monkeypatch.setattr(
        bounded_runner,
        "_signal_process_group",
        lambda *_args: pytest.fail("no signal is needed"),
    )

    assert bounded_runner._cleanup_process_group(42) is True


def test_cleanup_process_group_escalates_after_term_timeout(monkeypatch):
    signals = []
    waits = iter([False, True])
    monkeypatch.setattr(
        bounded_runner, "_process_group_alive", lambda _pgid: True
    )
    monkeypatch.setattr(
        bounded_runner,
        "_signal_process_group",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    monkeypatch.setattr(
        bounded_runner,
        "_wait_for_process_group_exit",
        lambda _pgid, _timeout: next(waits),
    )

    assert bounded_runner._cleanup_process_group(42) is True
    assert signals == [
        (42, signal.SIGTERM),
        (42, bounded_runner._force_kill_signal()),
    ]


def test_cleanup_command_processes_rechecks_group_after_reaping(monkeypatch):
    waits = []
    monkeypatch.setattr(
        bounded_runner, "_cleanup_process_group", lambda _pgid: False
    )
    monkeypatch.setattr(bounded_runner, "_cleanup_descendants", lambda: True)
    monkeypatch.setattr(
        bounded_runner,
        "_wait_for_process_group_exit",
        lambda pgid, timeout: waits.append((pgid, timeout)) or True,
    )

    assert (
        bounded_runner._cleanup_command_processes(42, track_descendants=True)
        is True
    )
    assert waits == [(42, bounded_runner.POLL_INTERVAL_S * 5)]


def test_cleanup_command_processes_reports_descendant_failure(monkeypatch):
    monkeypatch.setattr(bounded_runner, "_cleanup_descendants", lambda: False)

    assert (
        bounded_runner._cleanup_command_processes(None, track_descendants=True)
        is False
    )


def test_mirror_signal_resets_and_resends_signal(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bounded_runner.signal,
        "signal",
        lambda signum, handler: calls.append((signum, handler)),
    )
    monkeypatch.setattr(
        bounded_runner.os,
        "kill",
        lambda pid, signum: calls.append((pid, signum)),
    )
    monkeypatch.setattr(bounded_runner.os, "getpid", lambda: 42)

    assert bounded_runner._mirror_signal(signal.SIGTERM) == 143
    assert calls == [
        (signal.SIGTERM, signal.SIG_DFL),
        (42, signal.SIGTERM),
    ]


def test_run_bounded_command_reports_missing_shell(monkeypatch, capsys):
    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(bounded_runner, "_enable_child_subreaper", lambda: True)
    assert (
        bounded_runner.run_bounded_command("/missing/shell", "echo ok") == 127
    )
    assert "Unable to start configured shell" in capsys.readouterr().err


def test_run_bounded_command_fails_closed_when_procfs_is_unavailable(
    monkeypatch, capsys
):
    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(bounded_runner, "_enable_child_subreaper", lambda: True)
    monkeypatch.setattr(bounded_runner, "_descendants", lambda _pid: None)
    monkeypatch.setattr(
        bounded_runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("command must not start"),
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 125
    assert (
        "procfs descendant tracking is unavailable" in capsys.readouterr().err
    )


def test_run_bounded_command_reports_cleanup_failure(monkeypatch, capsys):
    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(bounded_runner, "_enable_child_subreaper", lambda: True)
    process = SimpleNamespace(pid=42, poll=lambda: 0, wait=lambda: 0)
    monkeypatch.setattr(
        bounded_runner.subprocess, "Popen", lambda _args, **_kwargs: process
    )
    monkeypatch.setattr(
        bounded_runner,
        "_cleanup_command_processes",
        lambda _pgid, *, track_descendants: False,
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 125
    assert "did not exit after forced termination" in capsys.readouterr().err


def test_run_bounded_command_mirrors_child_signal(monkeypatch):
    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(bounded_runner, "_enable_child_subreaper", lambda: True)
    process = SimpleNamespace(
        pid=42,
        poll=lambda: -signal.SIGTERM,
        wait=lambda: -signal.SIGTERM,
    )
    monkeypatch.setattr(
        bounded_runner.subprocess, "Popen", lambda _args, **_kwargs: process
    )
    monkeypatch.setattr(
        bounded_runner,
        "_cleanup_command_processes",
        lambda _pgid, *, track_descendants: True,
    )
    monkeypatch.setattr(
        bounded_runner, "_mirror_signal", lambda signum: 128 + signum
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 143


def test_run_bounded_command_mirrors_received_signal(monkeypatch):
    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(bounded_runner, "_enable_child_subreaper", lambda: True)
    handlers = {}
    process = SimpleNamespace(
        pid=42, poll=lambda: None, wait=lambda timeout=None: -signal.SIGTERM
    )
    monkeypatch.setattr(
        bounded_runner.subprocess, "Popen", lambda _args, **_kwargs: process
    )
    monkeypatch.setattr(
        bounded_runner, "_handled_signals", lambda: (signal.SIGTERM,)
    )
    monkeypatch.setattr(
        bounded_runner.signal,
        "signal",
        lambda signum, handler: handlers.setdefault(signum, handler),
    )
    monkeypatch.setattr(
        bounded_runner.time,
        "sleep",
        lambda _seconds: handlers[signal.SIGTERM](signal.SIGTERM, None),
    )
    monkeypatch.setattr(
        bounded_runner,
        "_cleanup_command_processes",
        lambda _pgid, *, track_descendants: True,
    )
    monkeypatch.setattr(
        bounded_runner, "_mirror_signal", lambda signum: 128 + signum
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 143


def test_run_bounded_command_returns_normal_exit_code(monkeypatch):
    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(bounded_runner, "_enable_child_subreaper", lambda: True)
    process = SimpleNamespace(pid=42, poll=lambda: 7, wait=lambda: 7)
    monkeypatch.setattr(
        bounded_runner.subprocess, "Popen", lambda _args, **_kwargs: process
    )
    monkeypatch.setattr(
        bounded_runner,
        "_cleanup_command_processes",
        lambda _pgid, *, track_descendants: True,
    )

    assert bounded_runner.run_bounded_command("sh", "false") == 7


def test_run_bounded_command_fails_closed_without_containment(
    monkeypatch, capsys
):
    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(
        bounded_runner, "_enable_child_subreaper", lambda: False
    )
    monkeypatch.setattr(
        bounded_runner, "_kqueue_descendant_tracking_available", lambda: False
    )
    monkeypatch.setattr(
        bounded_runner, "_process_group_containment_available", lambda: False
    )
    monkeypatch.setattr(
        bounded_runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("command must not start"),
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 125
    assert "descendant tracking is unavailable" in capsys.readouterr().err


def test_run_bounded_command_rejects_process_group_only_containment(
    monkeypatch, capsys
):
    monkeypatch.setattr(bounded_runner.sys, "platform", "linux")
    monkeypatch.setattr(
        bounded_runner, "_enable_child_subreaper", lambda: False
    )
    monkeypatch.setattr(
        bounded_runner, "_kqueue_descendant_tracking_available", lambda: False
    )
    monkeypatch.setattr(
        bounded_runner, "_process_group_containment_available", lambda: True
    )

    monkeypatch.setattr(
        bounded_runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("command must not start"),
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 125
    assert "descendant tracking is unavailable" in capsys.readouterr().err


def test_run_bounded_command_uses_kqueue_tracker(monkeypatch):
    monkeypatch.setattr(bounded_runner.sys, "platform", "freebsd14")
    monkeypatch.setattr(
        bounded_runner, "_process_group_containment_available", lambda: True
    )
    monkeypatch.setattr(
        bounded_runner, "_enable_child_subreaper", lambda: False
    )
    monkeypatch.setattr(
        bounded_runner, "_kqueue_descendant_tracking_available", lambda: True
    )
    process = SimpleNamespace(pid=42, poll=lambda: 0, wait=lambda: 0)
    tracker = SimpleNamespace(failed=False, close=lambda: None)
    monkeypatch.setattr(
        bounded_runner,
        "_spawn_kqueue_tracked_command",
        lambda shell, command, *, start_new_session: (process, tracker),
    )
    cleanup = []
    monkeypatch.setattr(
        bounded_runner,
        "_cleanup_kqueue_command_processes",
        lambda pgid, candidate, *, root_exited: (
            cleanup.append((pgid, candidate, root_exited)) or True
        ),
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 0
    assert cleanup == [(42, tracker, True)]


def test_run_bounded_command_fails_closed_on_kqueue_track_error(
    monkeypatch, capsys
):
    monkeypatch.setattr(bounded_runner.sys, "platform", "freebsd14")
    monkeypatch.setattr(
        bounded_runner, "_enable_child_subreaper", lambda: False
    )
    monkeypatch.setattr(
        bounded_runner, "_kqueue_descendant_tracking_available", lambda: True
    )

    class FakeProcess:
        pid = 42

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    class FakeTracker:
        failed = False

        def poll(self):
            self.failed = True

        def close(self):
            pass

    tracker = FakeTracker()
    monkeypatch.setattr(
        bounded_runner,
        "_spawn_kqueue_tracked_command",
        lambda *_args, **_kwargs: (FakeProcess(), tracker),
    )
    monkeypatch.setattr(
        bounded_runner,
        "_cleanup_kqueue_command_processes",
        lambda *_args, **_kwargs: False,
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 125
    assert "tracking failed during execution" in capsys.readouterr().err


def test_bounded_exec_gate_requires_release_byte(monkeypatch, capsys):
    closed = []
    monkeypatch.setattr(bounded_runner.os, "read", lambda _fd, _size: b"")
    monkeypatch.setattr(
        bounded_runner.os, "close", lambda fd: closed.append(fd)
    )
    monkeypatch.setattr(
        bounded_runner.os,
        "execvp",
        lambda *_args: pytest.fail("shell must not execute"),
    )

    assert bounded_runner._run_bounded_exec_gate(9, "sh", "true") == 125
    assert closed == [9]
    assert "registration failed" in capsys.readouterr().err


def test_bounded_exec_gate_releases_shell_with_path_lookup(monkeypatch):
    class ExecCalled(Exception):
        pass

    calls = []
    monkeypatch.setattr(bounded_runner.os, "read", lambda _fd, _size: b"\x01")
    monkeypatch.setattr(bounded_runner.os, "close", lambda _fd: None)

    def fake_execvp(shell, args):
        calls.append((shell, args))
        raise ExecCalled

    monkeypatch.setattr(bounded_runner.os, "execvp", fake_execvp)

    with pytest.raises(ExecCalled):
        bounded_runner._run_bounded_exec_gate(9, "sh", "echo ok")

    assert calls == [("sh", ["sh", "-lc", "echo ok"])]


def test_bounded_exec_gate_reports_read_and_exec_errors(monkeypatch, capsys):
    def fail_read(_fd, _size):
        raise OSError("read failed")

    monkeypatch.setattr(bounded_runner.os, "read", fail_read)
    monkeypatch.setattr(bounded_runner.os, "close", lambda _fd: None)
    assert bounded_runner._run_bounded_exec_gate(9, "sh", "true") == 125
    assert "read failed" in capsys.readouterr().err

    monkeypatch.setattr(bounded_runner.os, "read", lambda _fd, _size: b"\x01")

    def fail_exec(*_args):
        raise OSError("exec failed")

    monkeypatch.setattr(bounded_runner.os, "execvp", fail_exec)
    assert bounded_runner._run_bounded_exec_gate(9, "sh", "true") == 127
    assert "exec failed" in capsys.readouterr().err


def test_forked_process_poll_and_blocking_wait(monkeypatch):
    monkeypatch.setattr(bounded_runner.os, "WNOHANG", 1, raising=False)
    process = bounded_runner._ForkedProcess(42)
    waits = iter([(0, 0), (42, 7 << 8)])
    monkeypatch.setattr(
        bounded_runner.os, "waitpid", lambda _pid, _flags: next(waits)
    )
    assert process.poll() is None
    assert process.poll() == 7
    assert process.wait() == 7

    process = bounded_runner._ForkedProcess(43)
    monkeypatch.setattr(
        bounded_runner.os, "waitpid", lambda _pid, _flags: (43, 0)
    )
    assert process.wait() == 0


def test_forked_process_handles_reaped_timeout_and_kill(monkeypatch):
    monkeypatch.setattr(bounded_runner.os, "WNOHANG", 1, raising=False)
    process = bounded_runner._ForkedProcess(44)

    def already_reaped(*_args):
        raise ChildProcessError

    monkeypatch.setattr(bounded_runner.os, "waitpid", already_reaped)
    assert process.poll() is None
    assert process.wait() == 0

    process = bounded_runner._ForkedProcess(45)
    clock = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(bounded_runner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(process, "poll", lambda: None)
    monkeypatch.setattr(bounded_runner.time, "sleep", lambda _seconds: None)
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0.5)

    killed = []
    monkeypatch.setattr(
        bounded_runner.os, "kill", lambda pid, sig: killed.append((pid, sig))
    )
    process.kill()
    assert killed == [(45, bounded_runner._force_kill_signal())]

    def disappeared(*_args):
        raise ProcessLookupError

    monkeypatch.setattr(bounded_runner.os, "kill", disappeared)
    process.kill()


def test_spawn_kqueue_command_registers_before_release(monkeypatch):
    closed = []
    writes = []
    tracker = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(bounded_runner.os, "pipe", lambda: (10, 11))
    monkeypatch.setattr(bounded_runner.os, "fork", lambda: 42, raising=False)
    monkeypatch.setattr(
        bounded_runner.os, "close", lambda fd: closed.append(fd)
    )
    monkeypatch.setattr(
        bounded_runner.os,
        "write",
        lambda fd, payload: writes.append((fd, payload)) or len(payload),
    )
    monkeypatch.setattr(
        bounded_runner._KqueueDescendantTracker,
        "create",
        lambda _pid: tracker,
    )

    spawned = bounded_runner._spawn_kqueue_tracked_command(
        "sh", "true", start_new_session=True
    )

    assert spawned is not None
    process, returned_tracker = spawned
    assert process.pid == 42
    assert returned_tracker is tracker
    assert writes == [(11, b"\x01")]
    assert closed == [10, 11]


def test_spawn_kqueue_command_fails_on_tracker_registration(monkeypatch):
    class FakeProcess:
        pid = 42

        def __init__(self):
            self.killed = False

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("bounded", timeout)
            return 0

        def poll(self):
            return None if not self.killed else -signal.SIGKILL

        def kill(self):
            self.killed = True

    closed = []
    fake_process = FakeProcess()
    monkeypatch.setattr(bounded_runner.os, "pipe", lambda: (10, 11))
    monkeypatch.setattr(bounded_runner.os, "fork", lambda: 42, raising=False)
    monkeypatch.setattr(
        bounded_runner.os, "close", lambda fd: closed.append(fd)
    )
    monkeypatch.setattr(
        bounded_runner, "_ForkedProcess", lambda _pid: fake_process
    )

    def fail_registration(_pid):
        raise OSError("register failed")

    monkeypatch.setattr(
        bounded_runner._KqueueDescendantTracker,
        "create",
        fail_registration,
    )

    assert (
        bounded_runner._spawn_kqueue_tracked_command(
            "sh", "true", start_new_session=True
        )
        is None
    )
    assert fake_process.killed is True
    assert closed == [10, 11]


def test_spawn_kqueue_command_fails_on_gate_release(monkeypatch):
    class FakeProcess:
        pid = 42

        def __init__(self):
            self.killed = False

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("bounded", timeout)
            return 0

        def poll(self):
            return None if not self.killed else -signal.SIGKILL

        def kill(self):
            self.killed = True

    fake_process = FakeProcess()
    tracker = SimpleNamespace(closed=False)
    tracker.close = lambda: setattr(tracker, "closed", True)
    monkeypatch.setattr(bounded_runner.os, "pipe", lambda: (10, 11))
    monkeypatch.setattr(bounded_runner.os, "fork", lambda: 42, raising=False)
    monkeypatch.setattr(bounded_runner.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        bounded_runner, "_ForkedProcess", lambda _pid: fake_process
    )
    monkeypatch.setattr(
        bounded_runner._KqueueDescendantTracker,
        "create",
        lambda _pid: tracker,
    )

    def fail_release(_fd, _payload):
        raise OSError("release failed")

    monkeypatch.setattr(bounded_runner.os, "write", fail_release)

    assert (
        bounded_runner._spawn_kqueue_tracked_command(
            "sh", "true", start_new_session=True
        )
        is None
    )
    assert tracker.closed is True
    assert fake_process.killed is True


@pytest.mark.skipif(
    sys.platform != "darwin", reason="requires macOS launchd containment"
)
def test_macos_launchd_reaps_setsid_escape(tmp_path):
    pid_path = tmp_path / "escaped.pid"
    child_code = "import os,time; os.setsid(); time.sleep(30)"
    parent_code = (
        "import pathlib,subprocess,sys; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid), encoding='ascii')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workgate.ops.utils.bounded_runner",
            "--shell",
            "/bin/sh",
            "--command",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    escaped_pid = int(pid_path.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(escaped_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(escaped_pid, signal.SIGKILL)
        pytest.fail("setsid descendant escaped launchd cleanup")


def test_bounded_runner_argparse_handler_exits_with_result(monkeypatch):
    monkeypatch.setattr(
        bounded_runner, "run_bounded_command", lambda shell, command: 7
    )

    with pytest.raises(SystemExit, match="7"):
        bounded_runner.run_bounded_runner_from_args(
            argparse.Namespace(shell="sh", command="false")
        )


def test_bounded_runner_argparse_rejects_incomplete_launchd_child_args():
    with pytest.raises(SystemExit, match="125"):
        bounded_runner.run_bounded_runner_from_args(
            argparse.Namespace(
                shell="sh",
                command="true",
                launchd_child_status="status.json",
                launchd_child_started=None,
                launchd_child_stdout=None,
                launchd_child_stderr=None,
            )
        )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX FIFOs")
def test_launchd_child_streams_output_and_publishes_status(tmp_path):
    stdout_path = tmp_path / "stdout.fifo"
    stderr_path = tmp_path / "stderr.fifo"
    started_path = tmp_path / "started"
    status_path = tmp_path / "status.json"
    os.mkfifo(stdout_path, 0o600)
    os.mkfifo(stderr_path, 0o600)
    stdout_fd = os.open(stdout_path, os.O_RDONLY | os.O_NONBLOCK)
    stderr_fd = os.open(stderr_path, os.O_RDONLY | os.O_NONBLOCK)
    stdout_guard = os.open(stdout_path, os.O_WRONLY | os.O_NONBLOCK)
    stderr_guard = os.open(stderr_path, os.O_WRONLY | os.O_NONBLOCK)
    try:
        assert (
            bounded_runner._run_launchd_child(
                "/bin/sh",
                "printf child-out; printf child-err >&2; exit 7",
                status_path=status_path,
                started_path=started_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            == 0
        )
        assert started_path.read_text(encoding="ascii") == "started"
        assert bounded_runner._read_launchd_status(status_path) == 7
        assert os.read(stdout_fd, 1024) == b"child-out"
        assert os.read(stderr_fd, 1024) == b"child-err"
    finally:
        os.close(stdout_guard)
        os.close(stderr_guard)
        os.close(stdout_fd)
        os.close(stderr_fd)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX FIFOs")
def test_launchd_child_waits_for_coalition_release(tmp_path, monkeypatch):
    stdout_path = tmp_path / "stdout.fifo"
    stderr_path = tmp_path / "stderr.fifo"
    started_path = tmp_path / "started"
    status_path = tmp_path / "status.json"
    coalition_path = tmp_path / "coalition.json"
    release_path = tmp_path / "release"
    os.mkfifo(stdout_path, 0o600)
    os.mkfifo(stderr_path, 0o600)
    stdout_fd = os.open(stdout_path, os.O_RDONLY | os.O_NONBLOCK)
    stderr_fd = os.open(stderr_path, os.O_RDONLY | os.O_NONBLOCK)
    stdout_guard = os.open(stdout_path, os.O_WRONLY | os.O_NONBLOCK)
    stderr_guard = os.open(stderr_path, os.O_WRONLY | os.O_NONBLOCK)
    monkeypatch.setattr(
        bounded_runner, "_macos_process_coalition_ids", lambda _pid: (202, 22)
    )
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            bounded_runner._run_launchd_child(
                "/bin/sh",
                "printf gated-output",
                status_path=status_path,
                started_path=started_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                coalition_path=coalition_path,
                release_path=release_path,
            )
        ),
        daemon=True,
    )
    try:
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if coalition_path.exists() and started_path.exists():
                break
            time.sleep(0.01)
        else:
            pytest.fail("launchd child did not publish its coalition gate")

        assert bounded_runner._read_launchd_coalition(coalition_path) == (
            202,
            22,
        )
        assert not status_path.exists()
        assert thread.is_alive()

        bounded_runner._write_private_json(release_path, {"release": True})
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert result == [0]
        assert bounded_runner._read_launchd_status(status_path) == 0
        assert os.read(stdout_fd, 1024) == b"gated-output"
        with pytest.raises(BlockingIOError):
            os.read(stderr_fd, 1024)
    finally:
        os.close(stdout_guard)
        os.close(stderr_guard)
        os.close(stdout_fd)
        os.close(stderr_fd)


def test_run_bounded_command_routes_macos_through_launchd(monkeypatch):
    monkeypatch.setattr(bounded_runner.sys, "platform", "darwin")
    monkeypatch.setattr(
        bounded_runner, "_launchd_containment_available", lambda: True
    )
    monkeypatch.setattr(
        bounded_runner,
        "_run_launchd_bounded_command",
        lambda shell, command: (
            23 if (shell, command) == ("sh", "false") else 99
        ),
    )

    assert bounded_runner.run_bounded_command("sh", "false") == 23


def test_run_bounded_command_fails_closed_without_launchd(monkeypatch, capsys):
    monkeypatch.setattr(bounded_runner.sys, "platform", "darwin")
    monkeypatch.setattr(
        bounded_runner, "_launchd_containment_available", lambda: False
    )
    monkeypatch.setattr(
        bounded_runner,
        "_run_launchd_bounded_command",
        lambda *_args: pytest.fail("command must not start without launchd"),
    )

    assert bounded_runner.run_bounded_command("sh", "true") == 125
    assert "launchd containment is unavailable" in capsys.readouterr().err


def test_read_launchd_status_fails_closed_on_invalid_payload(tmp_path):
    status_path = tmp_path / "status.json"
    assert bounded_runner._read_launchd_status(status_path) is None

    status_path.write_text("{broken", encoding="utf-8")
    assert bounded_runner._read_launchd_status(status_path) == 125

    status_path.write_text('{"returncode":true}', encoding="utf-8")
    assert bounded_runner._read_launchd_status(status_path) == 125

    status_path.write_text('{"returncode":"0"}', encoding="utf-8")
    assert bounded_runner._read_launchd_status(status_path) == 125


def test_read_launchd_coalition_validates_resource_identity(tmp_path):
    coalition_path = tmp_path / "coalition.json"
    assert bounded_runner._read_launchd_coalition(coalition_path) is None

    coalition_path.write_text("{broken", encoding="utf-8")
    assert bounded_runner._read_launchd_coalition(coalition_path) == (0, 0)

    coalition_path.write_text('{"coalition_ids":[123,456]}', encoding="utf-8")
    assert bounded_runner._read_launchd_coalition(coalition_path) == (123, 456)

    coalition_path.write_text('{"coalition_ids":[0,456]}', encoding="utf-8")
    assert bounded_runner._read_launchd_coalition(coalition_path) == (0, 0)


def test_macos_libproc_enumerates_uid_and_reads_coalition(monkeypatch):
    class FakeLibproc:
        def proc_listpids(self, _kind, _uid, buffer, _size):
            if buffer is None:
                return 2 * bounded_runner.ctypes.sizeof(
                    bounded_runner.ctypes.c_int
                )
            buffer[0] = 111
            buffer[1] = 222
            return 2 * bounded_runner.ctypes.sizeof(bounded_runner.ctypes.c_int)

        def proc_pidinfo(self, pid, _flavor, _arg, buffer, size):
            info = bounded_runner.ctypes.cast(
                buffer,
                bounded_runner.ctypes.POINTER(
                    bounded_runner._ProcPidCoalitionInfo
                ),
            ).contents
            info.coalition_id[0] = pid + 1000
            info.coalition_id[1] = pid + 2000
            return size

    monkeypatch.setattr(bounded_runner, "_macos_libproc", lambda: FakeLibproc())
    monkeypatch.setattr(bounded_runner.os, "getuid", lambda: 501, raising=False)

    assert bounded_runner._macos_uid_pids() == [111, 222]
    assert bounded_runner._macos_process_coalition_ids(111) == (1111, 2111)


def test_macos_matching_coalition_ignores_exited_processes(monkeypatch):
    current = os.getpid()
    monkeypatch.setattr(
        bounded_runner, "_macos_uid_pids", lambda: [current, 11, 12, 13]
    )
    monkeypatch.setattr(
        bounded_runner,
        "_macos_process_coalition_ids",
        lambda pid: {11: (77, 1), 12: (88, 2), 13: None}[pid],
    )

    def fake_kill(pid, sig):
        assert sig == 0
        if pid == 13:
            raise ProcessLookupError

    monkeypatch.setattr(bounded_runner.os, "kill", fake_kill)

    assert bounded_runner._macos_matching_coalition_pids((77, 9)) == {11}


def test_terminate_macos_coalition_repeatedly_signals_new_members(monkeypatch):
    snapshots = iter(({11}, {11, 12}, {12}, set()))
    monkeypatch.setattr(
        bounded_runner,
        "_macos_matching_coalition_pids",
        lambda _ids: next(snapshots),
    )
    monkeypatch.setattr(bounded_runner.time, "sleep", lambda _delay: None)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        bounded_runner.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert bounded_runner._terminate_macos_coalition(
        (77, 9), signal.SIGTERM, 1.0
    )
    assert signals == [
        (11, signal.SIGTERM),
        (12, signal.SIGTERM),
        (11, signal.SIGTERM),
        (12, signal.SIGTERM),
    ]


def test_launchctl_operations_reserve_outer_cleanup_headroom(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(bounded_runner.subprocess, "run", fake_run)

    bounded_runner._launchctl(["bootstrap", "user/1000", "job.plist"])
    bounded_runner._launchctl(["bootout", "user/1000/example"])

    assert calls[0][1]["timeout"] == bounded_runner.LAUNCHD_CONTROL_TIMEOUT_S
    assert calls[1][1]["timeout"] == bounded_runner.LAUNCHD_CONTROL_TIMEOUT_S
    assert (
        2
        * (
            bounded_runner.LAUNCHD_CONTROL_TIMEOUT_S
            + bounded_runner.LAUNCHD_CLEANUP_GRACE_S
            + bounded_runner.MACOS_TERMINATE_GRACE_S
            + bounded_runner.MACOS_KILL_GRACE_S
        )
        < 5.0
    )


def test_bounded_runner_argv_uses_frozen_cli_subcommand(monkeypatch):
    monkeypatch.setattr(bounded_runner.sys, "executable", "/app/workgate")

    assert bounded_runner.bounded_runner_argv(
        "/bin/sh",
        "echo ok",
        "--launchd-child-status",
        "/tmp/status.json",
        frozen=True,
    ) == [
        "/app/workgate",
        "bounded-runner",
        "--shell",
        "/bin/sh",
        "--command",
        "echo ok",
        "--launchd-child-status",
        "/tmp/status.json",
    ]


def test_bounded_runner_argv_uses_platform_native_absolute_script(monkeypatch):
    monkeypatch.setattr(
        bounded_runner.Path,
        "resolve",
        lambda _self: pytest.fail(
            "runner argv must not depend on pathlib flavor"
        ),
    )

    argv = bounded_runner.bounded_runner_argv(
        "/bin/sh", "echo ok", frozen=False
    )

    assert argv[0] == bounded_runner.sys.executable
    assert os.path.isabs(argv[1])
    assert argv[1].endswith("bounded_runner.py")
    assert argv[2:] == [
        "--shell",
        "/bin/sh",
        "--command",
        "echo ok",
    ]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX FIFOs")
def test_launchd_plist_uses_frozen_cli_subcommand(monkeypatch):
    _install_fake_launchd_coalitions(monkeypatch)
    program_arguments: list[str] = []
    monkeypatch.setattr(bounded_runner.sys, "executable", "/app/workgate")
    monkeypatch.setattr(bounded_runner.sys, "frozen", True, raising=False)

    def fake_launchctl(args):
        if args[0] == "bootstrap":
            with open(args[2], "rb") as handle:
                payload = plistlib.load(handle)
            program_arguments.extend(payload["ProgramArguments"])
            raise subprocess.TimeoutExpired(args, 0.01)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        bounded_runner, "_launchd_domain_targets", lambda: ("user/1000",)
    )
    monkeypatch.setattr(bounded_runner, "_launchctl", fake_launchctl)
    monkeypatch.setattr(bounded_runner, "LAUNCHD_CLEANUP_GRACE_S", 0.0)

    assert bounded_runner._run_launchd_bounded_command("/bin/sh", "true") == 125
    assert program_arguments[:6] == [
        "/app/workgate",
        "bounded-runner",
        "--shell",
        "/bin/sh",
        "--command",
        "true",
    ]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX FIFOs")
def test_launchd_parent_cleans_uncertain_timed_out_bootstrap(
    monkeypatch, capsys
):
    _install_fake_launchd_coalitions(monkeypatch)
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(args):
        calls.append(tuple(args))
        if args[0] == "bootstrap":
            raise subprocess.TimeoutExpired(args, 0.01)
        assert args[0] == "bootout"
        if sum(call[0] == "bootout" for call in calls) == 1:
            return subprocess.CompletedProcess(args, 5, "", "cleanup stalled")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        bounded_runner, "_launchd_domain_targets", lambda: ("user/1000",)
    )
    monkeypatch.setattr(bounded_runner, "_launchctl", fake_launchctl)
    monkeypatch.setattr(bounded_runner, "LAUNCHD_CLEANUP_GRACE_S", 0.0)

    assert bounded_runner._run_launchd_bounded_command("/bin/sh", "true") == 125
    assert [call[0] for call in calls] == ["bootstrap", "bootout", "bootout"]
    assert calls[1][1] == calls[2][1]
    assert "registration failed" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX FIFOs")
def test_launchd_parent_forwards_output_and_real_status(monkeypatch, capsys):
    _install_fake_launchd_coalitions(monkeypatch)
    child_threads: list[threading.Thread] = []

    def fake_launchctl(args):
        if args[0] == "bootstrap":
            with open(args[2], "rb") as handle:
                payload = plistlib.load(handle)
            arguments = payload["ProgramArguments"]
            values = {
                arguments[index]: arguments[index + 1]
                for index in range(len(arguments) - 1)
                if str(arguments[index]).startswith("--launchd-child-")
            }
            thread = threading.Thread(
                target=bounded_runner._run_launchd_child,
                args=(
                    arguments[arguments.index("--shell") + 1],
                    arguments[arguments.index("--command") + 1],
                ),
                kwargs={
                    "status_path": bounded_runner.Path(
                        values["--launchd-child-status"]
                    ),
                    "started_path": bounded_runner.Path(
                        values["--launchd-child-started"]
                    ),
                    "stdout_path": bounded_runner.Path(
                        values["--launchd-child-stdout"]
                    ),
                    "stderr_path": bounded_runner.Path(
                        values["--launchd-child-stderr"]
                    ),
                    "coalition_path": bounded_runner.Path(
                        values["--launchd-child-coalition"]
                    ),
                    "release_path": bounded_runner.Path(
                        values["--launchd-child-release"]
                    ),
                },
                daemon=True,
            )
            child_threads.append(thread)
            thread.start()
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args[0] == "bootout"
        for thread in child_threads:
            thread.join(timeout=2)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        bounded_runner, "_launchd_domain_targets", lambda: ("user/1000",)
    )
    monkeypatch.setattr(bounded_runner, "_launchctl", fake_launchctl)
    monkeypatch.setattr(bounded_runner, "LAUNCHD_CLEANUP_GRACE_S", 0.0)

    assert (
        bounded_runner._run_launchd_bounded_command(
            "/bin/sh", "printf parent-out; printf parent-err >&2; exit 9"
        )
        == 9
    )
    captured = capsys.readouterr()
    assert captured.out == "parent-out"
    assert captured.err == "parent-err"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX FIFOs")
def test_launchd_parent_falls_back_to_user_domain(monkeypatch):
    _install_fake_launchd_coalitions(monkeypatch)
    child_threads: list[threading.Thread] = []
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(args):
        calls.append(tuple(args))
        if args[:2] == ["bootstrap", "gui/1000"]:
            return subprocess.CompletedProcess(args, 5, "", "gui unavailable")
        if args[:2] == ["bootstrap", "user/1000"]:
            with open(args[2], "rb") as handle:
                payload = plistlib.load(handle)
            arguments = payload["ProgramArguments"]
            values = {
                arguments[index]: arguments[index + 1]
                for index in range(len(arguments) - 1)
                if str(arguments[index]).startswith("--launchd-child-")
            }
            thread = threading.Thread(
                target=bounded_runner._run_launchd_child,
                args=(
                    arguments[arguments.index("--shell") + 1],
                    arguments[arguments.index("--command") + 1],
                ),
                kwargs={
                    "status_path": bounded_runner.Path(
                        values["--launchd-child-status"]
                    ),
                    "started_path": bounded_runner.Path(
                        values["--launchd-child-started"]
                    ),
                    "stdout_path": bounded_runner.Path(
                        values["--launchd-child-stdout"]
                    ),
                    "stderr_path": bounded_runner.Path(
                        values["--launchd-child-stderr"]
                    ),
                    "coalition_path": bounded_runner.Path(
                        values["--launchd-child-coalition"]
                    ),
                    "release_path": bounded_runner.Path(
                        values["--launchd-child-release"]
                    ),
                },
                daemon=True,
            )
            child_threads.append(thread)
            thread.start()
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args[0] == "bootout"
        for thread in child_threads:
            thread.join(timeout=2)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        bounded_runner,
        "_launchd_domain_targets",
        lambda: ("gui/1000", "user/1000"),
    )
    monkeypatch.setattr(bounded_runner, "_launchctl", fake_launchctl)
    monkeypatch.setattr(bounded_runner, "LAUNCHD_CLEANUP_GRACE_S", 0.0)

    assert bounded_runner._run_launchd_bounded_command("/bin/sh", "exit 0") == 0
    assert calls[0][:2] == ("bootstrap", "gui/1000")
    assert calls[1][:2] == ("bootstrap", "user/1000")
    assert calls[2][0] == "bootout"
    assert calls[2][1].startswith("user/1000/workgate.bounded.")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX FIFOs")
def test_launchd_parent_fails_closed_when_cleanup_is_unconfirmed(
    monkeypatch, capsys
):
    _install_fake_launchd_coalitions(monkeypatch)
    child_threads: list[threading.Thread] = []
    bootout_calls = 0

    def fake_launchctl(args):
        nonlocal bootout_calls
        if args[0] == "bootstrap":
            with open(args[2], "rb") as handle:
                payload = plistlib.load(handle)
            arguments = payload["ProgramArguments"]
            values = {
                arguments[index]: arguments[index + 1]
                for index in range(len(arguments) - 1)
                if str(arguments[index]).startswith("--launchd-child-")
            }
            thread = threading.Thread(
                target=bounded_runner._run_launchd_child,
                args=(
                    arguments[arguments.index("--shell") + 1],
                    arguments[arguments.index("--command") + 1],
                ),
                kwargs={
                    "status_path": bounded_runner.Path(
                        values["--launchd-child-status"]
                    ),
                    "started_path": bounded_runner.Path(
                        values["--launchd-child-started"]
                    ),
                    "stdout_path": bounded_runner.Path(
                        values["--launchd-child-stdout"]
                    ),
                    "stderr_path": bounded_runner.Path(
                        values["--launchd-child-stderr"]
                    ),
                    "coalition_path": bounded_runner.Path(
                        values["--launchd-child-coalition"]
                    ),
                    "release_path": bounded_runner.Path(
                        values["--launchd-child-release"]
                    ),
                },
                daemon=True,
            )
            child_threads.append(thread)
            thread.start()
            return subprocess.CompletedProcess(args, 0, "", "")
        bootout_calls += 1
        for thread in child_threads:
            thread.join(timeout=2)
        return subprocess.CompletedProcess(args, 5, "", "cleanup failed")

    monkeypatch.setattr(
        bounded_runner, "_launchd_domain_targets", lambda: ("user/1000",)
    )
    monkeypatch.setattr(bounded_runner, "_launchctl", fake_launchctl)
    monkeypatch.setattr(bounded_runner, "LAUNCHD_CLEANUP_GRACE_S", 0.0)

    assert (
        bounded_runner._run_launchd_bounded_command("/bin/sh", "exit 0") == 125
    )
    assert "cleanup failed" in capsys.readouterr().err
    assert bootout_calls == 2


def test_bounded_runner_main_parses_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bounded_runner,
        "run_bounded_command",
        lambda shell, command: calls.append((shell, command)) or 4,
    )
    monkeypatch.setattr(
        bounded_runner.sys,
        "argv",
        ["bounded-runner", "--shell", "sh", "--command", "echo ok"],
    )

    with pytest.raises(SystemExit, match="4"):
        bounded_runner.main()

    assert calls == [("sh", "echo ok")]
