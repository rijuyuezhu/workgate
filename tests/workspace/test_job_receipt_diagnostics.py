"""Fault-injected durable Job receipt diagnostics."""

import sys
from pathlib import Path

import pytest

from workgate.executor.jobs import runner as job_runner
from workgate.executor.jobs import runner_bootstrap
from workgate.jobs import persistence, status


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (None, "missing"),
        ("{invalid", "invalid_json"),
        ('["not an object"]', "invalid_shape"),
        ('{"exit_code": 0}', "valid"),
    ],
)
def test_lost_transition_audits_receipt_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: str | None,
    expected: str,
) -> None:
    path = tmp_path / "attempt.status.json"
    if data is not None:
        path.write_text(data)
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        status, "audit", lambda event, **fields: events.append((event, fields))
    )
    job = {
        "job_id": "job_123",
        "kind": "shell",
        "status": "running",
        "session_id": "sess_123",
        "shell_id": "gone",
        "attempts": 1,
        "status_path": str(path),
    }
    for now in (15.0, 16.0):
        # Stub the first read to model a receipt that appears during the
        # diagnostic snapshot, while leaving reconciliation pure.
        status._refresh_job_status(
            job,
            set(),
            now=now,
            managed_job_has_local_task=lambda _j: False,
            managed_job_liveness=lambda _j: "unknown",
            read_status=lambda _j: None,
        )
    assert job["status"] == "lost"
    assert events == [
        (
            "job_completion_receipt_unavailable",
            {
                "job_id": "job_123",
                "session": "sess_123",
                "attempt": 1,
                "receipt_state": expected,
            },
        )
    ]


def test_receipt_classifies_unreadable_missing_and_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bad.status.json"
    path.write_bytes(b"\xff")
    assert status._missing_receipt_reason(path) == "invalid_encoding"
    assert status._missing_receipt_reason(None) == "no_path"

    def deny(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("synthetic failure")

    monkeypatch.setattr(Path, "read_text", deny)
    assert status._missing_receipt_reason(path) == "unreadable"


def test_audit_failure_does_not_interrupt_job_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("audit store unwritable")

    monkeypatch.setattr(status, "audit", fail)
    job = {
        "kind": "shell",
        "status": "running",
        "shell_id": "gone",
        "status_path": str(tmp_path / "missing"),
    }
    status._refresh_job_status(
        job,
        set(),
        now=15.0,
        managed_job_has_local_task=lambda _j: False,
        managed_job_liveness=lambda _j: "unknown",
    )
    assert job["status"] == "lost"


def test_bootstrap_keeps_unhandled_runner_failure_and_prunes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "job.status.json"
    monkeypatch.setattr(
        sys, "argv", ["runner_bootstrap.py", "--status-file", str(receipt)]
    )
    monkeypatch.setattr(sys, "path", sys.path.copy())

    def fail() -> None:
        raise RuntimeError("injected status write failure")

    monkeypatch.setattr(job_runner, "main", fail)
    with pytest.raises(RuntimeError, match="injected status write failure"):
        runner_bootstrap.main()
    trace = receipt.with_suffix(".runner-stderr.log")
    assert "injected status write failure" in trace.read_text()
    assert trace.stat().st_mode & 0o777 == 0o600
    persistence.remove_attempt_paths(
        {
            "command": tmp_path / "job.command",
            "log": tmp_path / "job.log",
            "status": receipt,
        }
    )
    assert not trace.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["runner_bootstrap.py"],
        ["runner_bootstrap.py", "--status-file"],
    ],
)
def test_runner_bootstrap_without_receipt_argument_still_raises_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "path", sys.path.copy())

    def fail() -> None:
        raise RuntimeError("original import or runner error")

    monkeypatch.setattr(job_runner, "main", fail)
    with pytest.raises(RuntimeError, match="original import or runner error"):
        runner_bootstrap.main()
    assert not list(tmp_path.glob("*.runner-stderr.log"))
