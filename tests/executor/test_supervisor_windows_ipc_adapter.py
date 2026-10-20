"""Exercise the shared private IPC adapter without requiring a Windows runner."""

import argparse
import os
import socket
from types import SimpleNamespace

import pytest

from workgate import supervisor


def test_borrowed_child_socket_preserves_original(monkeypatch):
    parent, child = socket.socketpair()
    try:
        with supervisor._borrow_child_socket(child.fileno()) as borrowed:
            borrowed.sendall(b"hello")
            assert parent.recv(5) == b"hello"
        # Closing the borrowed wrapper must not close the role's socket.
        child.sendall(b"still-open")
        assert parent.recv(10) == b"still-open"
        monkeypatch.setenv(supervisor.PARENT_FD_ENV, str(child.fileno()))
        monkeypatch.setattr(
            supervisor, "os", SimpleNamespace(name="nt", environ=os.environ)
        )
        assert supervisor.is_child()
    finally:
        child.close()
        parent.close()
    assert not supervisor.is_child()


def test_windows_supervisor_refuses_job_failure_before_launch(monkeypatch):
    from workgate.platform import windows_job

    class RefusedJob:
        closed = False

        def attach_current_process(self):
            raise OSError("cannot attach existing job")

        def close(self):
            self.closed = True

    refused = RefusedJob()
    monkeypatch.setattr(
        supervisor, "os", SimpleNamespace(name="nt", environ={})
    )
    monkeypatch.setattr(windows_job, "WindowsJob", lambda: refused)
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("uncontained role spawned"),
    )
    with pytest.raises(OSError, match="cannot attach"):
        supervisor.supervise([supervisor.ChildCommand("control", ("unused",))])
    assert refused.closed


def test_windows_supervised_child_owns_descendant_job_once(monkeypatch):
    from workgate.platform import windows_job

    class OwnedJob:
        attachments = 0

        def attach_current_process(self):
            self.attachments += 1

    job = OwnedJob()
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(supervisor, "is_child", lambda: True)
    monkeypatch.setattr(supervisor, "_WINDOWS_CHILD_JOB", None)
    monkeypatch.setattr(windows_job, "WindowsJob", lambda: job)
    role = argparse.Namespace(_workgate_argv=["control"])
    assert not supervisor.supervise_cli_role("control", role)
    assert not supervisor.supervise_cli_role("control", role)
    assert job.attachments == 1
    assert supervisor._WINDOWS_CHILD_JOB is job


def test_windows_supervised_child_refuses_missing_containment(monkeypatch):
    from workgate.platform import windows_job

    class RefusedJob:
        closed = False

        def attach_current_process(self):
            raise OSError("child Job failure")

        def close(self):
            self.closed = True

    job = RefusedJob()
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(supervisor, "is_child", lambda: True)
    monkeypatch.setattr(supervisor, "_WINDOWS_CHILD_JOB", None)
    monkeypatch.setattr(windows_job, "WindowsJob", lambda: job)
    with pytest.raises(OSError, match="child Job failure"):
        supervisor.supervise_cli_role(
            "executor", argparse.Namespace(_workgate_argv=["executor", "run"])
        )
    assert job.closed
    assert supervisor._WINDOWS_CHILD_JOB is None
