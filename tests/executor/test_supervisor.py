"""Real POSIX subprocess tests for the CLI Parent/Child restart contract."""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from workgate import main, supervisor

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix FD IPC")

ROOT = Path(__file__).resolve().parents[2]
PARENT_CODE = """\
import json,sys
from workgate.supervisor import ChildCommand,supervise
import os, workgate.supervisor as supervisor
if os.environ.get('SUPERVISOR_SHORT_APPLY'): supervisor._APPLY_TIMEOUT_SECONDS = 1
roles = json.loads(sys.argv[1])
raise SystemExit(supervise([ChildCommand(role,tuple(argv)) for role,argv in roles]))
"""
CHILD_CODE = """\
import os,sys,time,signal,hashlib
from pathlib import Path
from workgate.supervisor import is_child,request_restart,notify_config_selected,notify_ready,restart_status
record,config,role,behavior=map(str,sys.argv[1:5])
if not is_child(): raise SystemExit(26)
if behavior == 'ignore-term': signal.signal(signal.SIGTERM, signal.SIG_IGN)
config_digest=hashlib.sha256(Path(config).read_bytes()).hexdigest()
with open(record, 'a') as out:
    out.write(f'{role} {os.getpid()} {os.getppid()} {os.environ["SUPERVISOR_TEST_ENV"]} {Path(config).read_text().strip()}\\n')
    out.flush()
if behavior == 'exit': raise SystemExit(7)
notify_config_selected(Path(config), config_digest)
(Path(record).parent / (role+'.registered')).touch()
if os.getenv('SUPERVISOR_READY_TEST'):
    if os.getenv('SUPERVISOR_DELAY_READY_ROLE') == role:
        while not (Path(record).parent / ('allow-ready-'+role)).exists():
            time.sleep(0.05)
    if Path(config).read_text().strip() in ('bad', 'bad-'+role): raise SystemExit(17)
    if Path(config).read_text().strip() in ('hang', 'hang-'+role):
        while True: time.sleep(0.05)
    notify_ready(Path(config), config_digest)
    with open(Path(record).parent / 'ready.log', 'a') as out:
        out.write(f'{role} {os.getpid()} {restart_status()}\\n')
        out.flush()
request_file = Path(record).parent / (role+'.restart')
while True:
    if request_file.exists():
        request_file.unlink()
        revision_file = Path(record).parent / (role+'.revision')
        try:
            request_restart(revision_file.read_text() if revision_file.exists() else None)
        except RuntimeError:
            (Path(record).parent / (role+'.rejected')).touch()
    time.sleep(0.05)
"""


def _launch(
    tmp_path: Path,
    roles: tuple[str, ...],
    *,
    exit_role: str = "",
    ignore_term_role: str = "",
    ready_test: bool = False,
    short_apply: bool = False,
    split_configs: bool = False,
    delayed_ready_role: str = "",
):
    config = tmp_path / "config.yaml"
    config.write_text("first")
    record = tmp_path / "children.log"
    commands = []
    for role in roles:
        role_config = tmp_path / f"{role}.yaml" if split_configs else config
        if split_configs:
            role_config.write_text("first")
        command = [
            sys.executable,
            "-u",
            "-c",
            CHILD_CODE,
            str(record),
            str(role_config),
            role,
            "exit"
            if role == exit_role
            else "ignore-term"
            if role == ignore_term_role or ignore_term_role == "both"
            else "wait",
        ]
        commands.append([role, command])
    env = os.environ.copy()
    env["SUPERVISOR_TEST_ENV"] = "parent-startup"
    if ready_test:
        env["SUPERVISOR_READY_TEST"] = "1"
    if short_apply:
        env["SUPERVISOR_SHORT_APPLY"] = "1"
    if delayed_ready_role:
        env["SUPERVISOR_DELAY_READY_ROLE"] = delayed_ready_role
    env["PYTHONPATH"] = (
        str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", PARENT_CODE, json.dumps(commands)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return (
        process,
        record,
        (tmp_path / f"{roles[0]}.yaml" if split_configs else config),
    )


def _lines(path: Path, number: int):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if path.exists():
            result = [line.split() for line in path.read_text().splitlines()]
            if len(result) >= number:
                return result
        time.sleep(0.05)
    pytest.fail(
        f"expected {number} Child startups; got {path.read_text() if path.exists() else '(none)'}"
    )


def _shutdown(process: subprocess.Popen[str]):
    if process.poll() is None:
        process.terminate()
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode == 143, (process.returncode, stdout, stderr)


def _pid_gone(pid: int):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_one_parent_restarts_only_child_with_original_env_and_new_file(
    tmp_path,
):
    process, record, config = _launch(tmp_path, ("control",))
    try:
        first = _lines(record, 1)[0]
        assert first[0] == "control"
        assert int(first[2]) == process.pid
        assert first[3:] == ["parent-startup", "first"]
        config.write_text("second")
        (tmp_path / "control.restart").touch()
        rows = _lines(record, 2)
        assert rows[1][0] == "control"
        assert rows[0][1] != rows[1][1]
        assert int(rows[1][2]) == process.pid
        assert rows[1][3:] == ["parent-startup", "second"]
    finally:
        _shutdown(process)
    for row in rows:
        _pid_gone(int(row[1]))


def test_colocate_parent_restarts_one_role_leaves_other_running(tmp_path):
    process, record, config = _launch(tmp_path, ("control", "executor"))
    try:
        rows = _lines(record, 2)
        assert {row[0] for row in rows} == {"control", "executor"}
        assert all(int(row[2]) == process.pid for row in rows)
        original_executor = next(row[1] for row in rows if row[0] == "executor")
        config.write_text("updated")
        (tmp_path / "control.restart").touch()
        rows = _lines(record, 3)
        assert rows[2][0] == "control"
        assert rows[2][4] == "updated"
        assert os.kill(int(original_executor), 0) is None
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in rows:
        _pid_gone(int(row[1]))


def test_unexpected_child_exit_stops_sibling_without_restart_loop(tmp_path):
    process, record, _ = _launch(
        tmp_path, ("control", "executor"), exit_role="control"
    )
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode == 7, (stdout, stderr)
    rows = _lines(record, 1)
    assert sum(row[0] == "control" for row in rows) == 1
    for row in rows:
        _pid_gone(int(row[1]))


def test_parent_sigint_cleans_up_child_and_returns_130(tmp_path):
    process, record, _ = _launch(tmp_path, ("control",))
    rows = _lines(record, 1)
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode == 130, (stdout, stderr)
    _pid_gone(int(rows[0][1]))


def test_parent_kills_unresponsive_child_after_bounded_grace(tmp_path):
    process, record, _ = _launch(
        tmp_path, ("executor",), ignore_term_role="executor"
    )
    rows = _lines(record, 1)
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode == 143, (stdout, stderr)
    _pid_gone(int(rows[0][1]))


def test_colocate_shutdown_has_one_bounded_grace_for_both_children(tmp_path):
    process, record, _ = _launch(
        tmp_path, ("control", "executor"), ignore_term_role="both"
    )
    rows = _lines(record, 2)
    started = time.monotonic()
    try:
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=9)
        elapsed = time.monotonic() - started
        assert process.returncode == 143, (stdout, stderr)
        # An outer service manager may kill the Parent after five seconds;
        # its two children cannot each consume the full three-second grace.
        assert elapsed < 5, f"Parent took {elapsed:.2f}s to stop both roles"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    for row in rows:
        _pid_gone(int(row[1]))


def test_repeated_sigterm_during_shutdown_still_reaps_child(tmp_path):
    process, record, _ = _launch(
        tmp_path, ("executor",), ignore_term_role="executor"
    )
    rows = _lines(record, 1)
    pid = int(rows[0][1])
    try:
        process.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        assert process.poll() is None  # Child is still within graceful stop.
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=9)
        assert process.returncode == 143, (stdout, stderr)
        _pid_gone(pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)  # Clean up even if the test fails.


def test_failed_second_spawn_reaps_first_child(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("first")
    record = tmp_path / "children.log"
    valid = [
        sys.executable,
        "-u",
        "-c",
        CHILD_CODE,
        str(record),
        str(config),
        "control",
        "wait",
    ]
    commands = [
        ["control", valid],
        ["executor", [str(tmp_path / "missing-executable")]],
    ]
    env = os.environ.copy()
    env["SUPERVISOR_TEST_ENV"] = "parent-startup"
    env["PYTHONPATH"] = (
        str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", PARENT_CODE, json.dumps(commands)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode != 0, (stdout, stderr)
    if record.exists():
        for row in _lines(record, 1):
            _pid_gone(int(row[1]))


def test_inherited_socket_not_environment_marker_is_required(monkeypatch):
    monkeypatch.setenv("WORKGATE_PARENT_IPC_FD", "999999")
    assert not supervisor.is_child()
    parent, child = socket.socketpair()
    try:
        monkeypatch.setenv("WORKGATE_PARENT_IPC_FD", str(child.fileno()))
        assert supervisor.is_child()
    finally:
        parent.close()
        child.close()
    assert not supervisor.is_child()
    with pytest.raises(RuntimeError, match="no Workgate Parent"):
        supervisor.request_restart()


def test_root_cli_supervises_only_running_roles(monkeypatch):
    calls = []
    monkeypatch.setattr(supervisor, "is_child", lambda: False)
    monkeypatch.setattr(
        supervisor, "run_parent", lambda commands: calls.append(commands)
    )
    main.main(["control", "--mode", "http"])
    main.main(["executor", "run"])
    assert [call[0].role for call in calls] == ["control", "executor"]
    assert calls[0][0].argv[-3:] == ("control", "--mode", "http")
    assert calls[1][0].argv[-2:] == ("executor", "run")


def test_apply_failure_restores_healthy_child_config_not_last_backup(tmp_path):
    process, record, config = _launch(tmp_path, ("control",), ready_test=True)
    try:
        ready_log = tmp_path / "ready.log"
        assert _lines(ready_log, 1)[0][2] == "healthy"
        assert _lines(record, 1)[0][4] == "first"
        # Two saves before Apply: the latest .bak is NOT last-known-good.
        config.write_text("intermediate")
        (tmp_path / "config.yaml.bak").write_text("intermediate")
        config.write_text("bad")
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        rows = _lines(ready_log, 2)
        assert rows[1][2] == "recovered"
        assert rows[0][1] != rows[1][1]
        assert config.read_text() == "first"
        assert (tmp_path / "config.yaml.bak").read_text() == "intermediate"
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in _lines(record, 3):
        _pid_gone(int(row[1]))


def test_rollback_refuses_to_overwrite_newer_concurrent_file(tmp_path):
    from workgate.supervisor import _GoodFile, _restore_good

    config = tmp_path / "config.yaml"
    config.write_text("changed again")
    assert not _restore_good(_GoodFile(config, b"last-good"), b"failed-apply")
    assert config.read_text() == "changed again"


def test_parent_rejects_stale_apply_revision_without_stopping_child(tmp_path):
    process, record, config = _launch(tmp_path, ("control",), ready_test=True)
    try:
        assert _lines(tmp_path / "ready.log", 1)[0][2] == "healthy"
        first = _lines(record, 1)[0]
        config.write_text("updated")
        (tmp_path / "control.revision").write_text("0" * 64)
        (tmp_path / "control.restart").touch()
        deadline = time.monotonic() + 6
        while (
            not (tmp_path / "control.rejected").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert (tmp_path / "control.rejected").exists()
        assert _lines(record, 1) == [first]
        assert process.poll() is None
        assert config.read_text() == "updated"
    finally:
        _shutdown(process)
    _pid_gone(int(first[1]))


def test_colocate_apply_failure_recovers_only_target_role(tmp_path):
    process, record, config = _launch(
        tmp_path,
        ("control", "executor"),
        ready_test=True,
        split_configs=True,
    )
    try:
        before = _lines(record, 2)
        assert len(_lines(tmp_path / "ready.log", 2)) == 2
        original_executor = next(row for row in before if row[0] == "executor")
        config.write_text("bad")
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        statuses = _lines(tmp_path / "ready.log", 3)
        assert statuses[-1][0] == "control"
        assert statuses[-1][2] == "recovered"
        assert config.read_text() == "first"
        assert process.poll() is None
        assert os.kill(int(original_executor[1]), 0) is None
        after = _lines(record, 4)
        assert [row[0] for row in after].count("executor") == 1
    finally:
        _shutdown(process)
    for row in after:
        _pid_gone(int(row[1]))


def test_apply_health_timeout_rolls_back_to_last_healthy_child(tmp_path):
    process, record, config = _launch(
        tmp_path, ("executor",), ready_test=True, short_apply=True
    )
    try:
        ready_log = tmp_path / "ready.log"
        assert _lines(ready_log, 1)[0][2] == "healthy"
        config.write_text("hang")
        (tmp_path / "executor.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / "executor.restart").touch()
        assert _lines(ready_log, 2)[-1][2] == "recovered"
        assert config.read_text() == "first"
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in _lines(record, 3):
        _pid_gone(int(row[1]))


@pytest.mark.parametrize("requester", ["control", "executor"])
def test_colocate_shared_file_applies_both_roles(tmp_path, requester):
    process, record, config = _launch(
        tmp_path, ("control", "executor"), ready_test=True
    )
    try:
        first = _lines(record, 2)
        assert len(_lines(tmp_path / "ready.log", 2)) == 2
        config.write_text("second")
        (tmp_path / f"{requester}.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / f"{requester}.restart").touch()
        after = _lines(record, 4)
        assert {row[0] for row in after[2:]} == {"control", "executor"}
        assert all(row[4] == "second" for row in after[2:])
        assert {row[1] for row in first}.isdisjoint(
            {row[1] for row in after[2:]}
        )
        statuses = _lines(tmp_path / "ready.log", 4)
        assert statuses[-1][2] == "healthy"
        assert all(
            row[2] in {"starting", "applying", "healthy"}
            for row in statuses[2:]
        )
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in after:
        _pid_gone(int(row[1]))


@pytest.mark.parametrize("failed_role", ["control", "executor"])
def test_colocate_shared_file_failure_rolls_back_both(tmp_path, failed_role):
    process, record, config = _launch(
        tmp_path, ("control", "executor"), ready_test=True
    )
    try:
        _lines(record, 2)
        assert len(_lines(tmp_path / "ready.log", 2)) == 2
        # The immediately preceding .bak is not necessarily last-known-good.
        config.write_text("intermediate")
        (tmp_path / "config.yaml.bak").write_text("intermediate")
        config.write_text("bad-" + failed_role)
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        statuses = _lines(tmp_path / "ready.log", 4)
        # A failed Child may exit before logging its candidate startup.
        # Both recovered roles, not a fixed candidate count, are the contract.
        after = _lines(record, 5)
        assert statuses[2][2] in {"starting", "applying"}  # Not success.
        assert all(row[4] == "first" for row in after[-2:])
        assert {row[0] for row in after[-2:]} == {"control", "executor"}
        assert statuses[-1][2] == "recovered"
        assert config.read_text() == "first"
        assert (tmp_path / "config.yaml.bak").read_text() == "intermediate"
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in after:
        _pid_gone(int(row[1]))


def test_colocate_shared_file_timeout_recovers_both(tmp_path):
    process, record, config = _launch(
        tmp_path, ("control", "executor"), ready_test=True, short_apply=True
    )
    try:
        assert len(_lines(tmp_path / "ready.log", 2)) == 2
        config.write_text("hang-executor")
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        after = _lines(record, 6)
        assert {row[0] for row in after[-2:]} == {"control", "executor"}
        statuses = _lines(tmp_path / "ready.log", 5)
        assert statuses[-1][2] == "recovered"
        assert config.read_text() == "first"
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in after:
        _pid_gone(int(row[1]))


def test_colocate_shared_file_rolls_back_to_latest_jointly_healthy_config(
    tmp_path,
):
    process, record, config = _launch(
        tmp_path, ("control", "executor"), ready_test=True
    )
    try:
        assert len(_lines(tmp_path / "ready.log", 2)) == 2
        config.write_text("second")
        (tmp_path / "executor.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / "executor.restart").touch()
        assert len(_lines(tmp_path / "ready.log", 4)) == 4
        config.write_text("bad-executor")
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        after = _lines(record, 8)
        statuses = _lines(tmp_path / "ready.log", 6)
        assert statuses[-1][2] == "recovered"
        assert {row[0] for row in after[-2:]} == {"control", "executor"}
        assert all(row[4] == "second" for row in after[-2:])
        assert config.read_text() == "second"
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in after:
        _pid_gone(int(row[1]))


def test_colocate_shared_file_waits_for_executor_ready_before_apply(tmp_path):
    process, record, config = _launch(
        tmp_path,
        ("control", "executor"),
        ready_test=True,
        delayed_ready_role="executor",
    )
    try:
        before = _lines(record, 2)
        assert _lines(tmp_path / "ready.log", 1)[0][0] == "control"
        config.write_text("second")
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(config.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        deadline = time.monotonic() + 5
        while (
            not (tmp_path / "control.rejected").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert (tmp_path / "control.rejected").exists()
        assert len(record.read_text().splitlines()) == 2
        assert process.poll() is None
        # Both roles must have fully loaded A before Apply may replace it.
        config.write_text("first")
        (tmp_path / "allow-ready-executor").touch()
        assert len(_lines(tmp_path / "ready.log", 2)) == 2
        config.write_text("second")
        (tmp_path / "control.restart").touch()
        after = _lines(record, 4)
        assert {row[0] for row in after[2:]} == {"control", "executor"}
        assert all(row[4] == "second" for row in after[2:])
        assert {row[1] for row in before}.isdisjoint(
            {row[1] for row in after[2:]}
        )
    finally:
        _shutdown(process)
    for row in after:
        _pid_gone(int(row[1]))


def test_colocate_independent_file_can_apply_while_executor_starts(tmp_path):
    process, record, control_file = _launch(
        tmp_path,
        ("control", "executor"),
        ready_test=True,
        split_configs=True,
        delayed_ready_role="executor",
    )
    try:
        _lines(record, 2)
        assert _lines(tmp_path / "ready.log", 1)[0][0] == "control"
        deadline = time.monotonic() + 5
        while (
            not (tmp_path / "executor.registered").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert (tmp_path / "executor.registered").exists()
        control_file.write_text("second")
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(control_file.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        after = _lines(record, 3)
        assert after[-1][0] == "control"
        assert after[-1][4] == "second"
        assert sum(row[0] == "executor" for row in after) == 1
        assert process.poll() is None
        (tmp_path / "allow-ready-executor").touch()
    finally:
        _shutdown(process)
    for row in after:
        _pid_gone(int(row[1]))


def test_colocate_rejects_second_apply_while_first_is_running(tmp_path):
    process, record, control_file = _launch(
        tmp_path, ("control", "executor"), ready_test=True, split_configs=True
    )
    try:
        _lines(record, 2)
        assert len(_lines(tmp_path / "ready.log", 2)) == 2
        control_file.write_text("hang-control")
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(control_file.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        after = _lines(record, 3)
        assert after[-1][0] == "control"
        executor_file = tmp_path / "executor.yaml"
        executor_file.write_text("second")
        (tmp_path / "executor.revision").write_text(
            hashlib.sha256(executor_file.read_bytes()).hexdigest()
        )
        (tmp_path / "executor.restart").touch()
        deadline = time.monotonic() + 5
        while (
            not (tmp_path / "executor.rejected").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert (tmp_path / "executor.rejected").exists()
        assert (
            sum(
                row.split()[0] == "executor"
                for row in record.read_text().splitlines()
            )
            == 1
        )
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in after:
        _pid_gone(int(row[1]))


def test_plain_restart_without_ready_has_no_apply_timeout(tmp_path):
    process, record, config = _launch(tmp_path, ("control",), short_apply=True)
    try:
        before = _lines(record, 1)
        config.write_text("second")
        (tmp_path / "control.restart").touch()
        after = _lines(record, 2)
        assert after[-1][4] == "second"
        # A plain restart is not a configuration Apply transaction.
        time.sleep(1.7)
        assert process.poll() is None
    finally:
        _shutdown(process)
    for row in before + after[1:]:
        _pid_gone(int(row[1]))


def test_unrelated_child_exit_does_not_rollback_another_role_apply(tmp_path):
    process, record, control_file = _launch(
        tmp_path,
        ("control", "executor"),
        ready_test=True,
        split_configs=True,
        short_apply=True,
    )
    try:
        before = _lines(record, 2)
        assert len(_lines(tmp_path / "ready.log", 2)) == 2
        executor_pid = int(
            next(row[1] for row in before if row[0] == "executor")
        )
        control_file.write_text("hang-control")
        (tmp_path / "control.revision").write_text(
            hashlib.sha256(control_file.read_bytes()).hexdigest()
        )
        (tmp_path / "control.restart").touch()
        after = _lines(record, 3)
        assert after[-1][0] == "control"
        os.kill(executor_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process.poll() == 137
        # An Executor crash must not roll back a Control-only Apply.
        assert control_file.read_text() == "hang-control"
        assert len(record.read_text().splitlines()) == 3
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=8)
    for row in after:
        _pid_gone(int(row[1]))
