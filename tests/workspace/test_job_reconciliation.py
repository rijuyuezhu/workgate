from typing import Any, cast

import pytest

from workgate.executor.jobs import lifecycle as job_lifecycle
from workgate.jobs.reconciliation import (
    JobObservation,
    JobTransition,
    reconcile_job,
)
from workgate.jobs.state import JobStatusPayload


def _status_payload(
    *,
    exit_code: int | None = 0,
    completed_at: float = 12.0,
    error: str | None = None,
) -> JobStatusPayload:
    return cast(
        JobStatusPayload,
        {
            "exit_code": exit_code,
            "completed_at": completed_at,
            "error": error,
            "log_truncated": False,
            "output_bytes": 7,
        },
    )


@pytest.mark.parametrize(
    ("job", "observation", "expected"),
    [
        pytest.param(
            {"kind": "shell", "status": "succeeded"},
            JobObservation(now=20.0, shell_liveness="absent"),
            JobTransition(),
            id="terminal-row-is-stable",
        ),
        pytest.param(
            {"kind": "shell", "status": "lost", "shell_id": "shell-1"},
            JobObservation(now=20.0),
            JobTransition(),
            id="lost-shell-unknown-inventory-is-stable",
        ),
        pytest.param(
            {
                "kind": "shell",
                "status": "lost",
                "shell_id": "shell-1",
                "shell_absence_confirmed": True,
            },
            JobObservation(now=20.0, shell_liveness="live"),
            JobTransition(
                updates={
                    "status": "running",
                    "updated_at": 20.0,
                    "completed_at": None,
                    "exit_code": None,
                    "error": "recovered a lost job whose shell is still active",
                },
                remove_keys=("shell_absence_confirmed",),
            ),
            id="lost-shell-recovers-from-live-inventory",
        ),
        pytest.param(
            {"kind": "shell", "status": "lost", "shell_id": "shell-1"},
            JobObservation(now=20.0, shell_liveness="absent"),
            JobTransition(updates={"shell_absence_confirmed": True}),
            id="lost-shell-records-authoritative-absence",
        ),
        pytest.param(
            {
                "kind": "shell",
                "status": "lost",
                "shell_absence_confirmed": True,
            },
            JobObservation(now=20.0, status_payload=_status_payload()),
            JobTransition(
                updates={
                    "status": "succeeded",
                    "updated_at": 12.0,
                    "completed_at": 12.0,
                    "exit_code": 0,
                    "error": None,
                    "log_truncated": False,
                    "output_bytes": 7,
                },
                remove_keys=("shell_absence_confirmed",),
                clear_operation=True,
            ),
            id="lost-shell-durable-completion-wins",
        ),
        pytest.param(
            {"kind": "managed", "status": "running"},
            JobObservation(
                now=20.0,
                managed_has_local_task=True,
                managed_liveness="dead",
            ),
            JobTransition(),
            id="managed-local-task-is-authoritative",
        ),
        pytest.param(
            {"kind": "managed", "status": "running"},
            JobObservation(now=20.0, managed_liveness="live"),
            JobTransition(),
            id="managed-cross-process-live-is-stable",
        ),
        pytest.param(
            {"kind": "managed", "status": "running"},
            JobObservation(now=20.0, managed_liveness="unknown"),
            JobTransition(),
            id="managed-unknown-liveness-is-conservative",
        ),
        pytest.param(
            {"kind": "managed", "status": "running"},
            JobObservation(now=20.0, managed_liveness="dead"),
            JobTransition(
                updates={
                    "status": "lost",
                    "updated_at": 20.0,
                    "completed_at": 20.0,
                    "exit_code": None,
                    "error": "managed job is no longer running; retry it to resume the operation",
                },
                clear_operation=True,
            ),
            id="managed-dead-running-becomes-lost",
        ),
        pytest.param(
            {"kind": "managed", "status": "stopping"},
            JobObservation(now=20.0, managed_liveness="dead"),
            JobTransition(
                updates={
                    "status": "stopped",
                    "updated_at": 20.0,
                    "completed_at": 20.0,
                    "exit_code": None,
                    "error": None,
                },
                clear_operation=True,
            ),
            id="managed-dead-stopping-becomes-stopped",
        ),
        pytest.param(
            {"kind": "shell", "status": "starting"},
            JobObservation(
                now=20.0,
                shell_liveness="absent",
                active_operation="start",
            ),
            JobTransition(),
            id="active-start-operation-blocks-reconciliation",
        ),
        pytest.param(
            {"kind": "shell", "status": "starting", "last_started_at": 8.0},
            JobObservation(now=20.0, shell_liveness="live"),
            JobTransition(
                updates={
                    "status": "running",
                    "updated_at": 20.0,
                    "last_started_at": 8.0,
                    "completed_at": None,
                    "exit_code": None,
                    "error": "recovered job start after an interrupted state commit",
                },
                clear_operation=True,
            ),
            id="interrupted-start-recovers-live-shell",
        ),
        pytest.param(
            {"kind": "shell", "status": "starting"},
            JobObservation(now=20.0, shell_liveness="absent"),
            JobTransition(
                updates={
                    "status": "failed",
                    "updated_at": 20.0,
                    "completed_at": 20.0,
                    "exit_code": None,
                    "error": "job start was interrupted before a recoverable shell was created",
                },
                clear_operation=True,
            ),
            id="interrupted-start-without-shell-fails",
        ),
        pytest.param(
            {
                "kind": "shell",
                "status": "retrying",
                "pending_attempt": 3,
                "pending_shell_id": "shell-3",
                "pending_log_path": "/tmp/log-3",
                "pending_status_path": "/tmp/status-3",
            },
            JobObservation(
                now=20.0,
                pending_status_payload=_status_payload(
                    exit_code=1, error="boom"
                ),
            ),
            JobTransition(
                updates={
                    "attempts": 3,
                    "shell_id": "shell-3",
                    "log_path": "/tmp/log-3",
                    "status_path": "/tmp/status-3",
                    "status": "failed",
                    "updated_at": 12.0,
                    "completed_at": 12.0,
                    "exit_code": 1,
                    "error": "boom",
                    "log_truncated": False,
                    "output_bytes": 7,
                },
                remove_keys=(
                    "pending_attempt",
                    "pending_shell_id",
                    "pending_command_path",
                    "pending_log_path",
                    "pending_status_path",
                    "shell_absence_confirmed",
                ),
                clear_operation=True,
            ),
            id="retry-completion-adopts-pending-attempt",
        ),
        pytest.param(
            {
                "kind": "shell",
                "status": "retrying",
                "pending_attempt": 3,
                "pending_shell_id": "shell-3",
            },
            JobObservation(now=20.0, shell_liveness="live"),
            JobTransition(
                updates={
                    "attempts": 3,
                    "shell_id": "shell-3",
                    "status": "running",
                    "updated_at": 20.0,
                    "last_started_at": 20.0,
                    "completed_at": None,
                    "exit_code": None,
                    "error": "recovered retry after an interrupted state commit",
                },
                remove_keys=(
                    "pending_attempt",
                    "pending_shell_id",
                    "pending_command_path",
                    "pending_log_path",
                    "pending_status_path",
                ),
                clear_operation=True,
            ),
            id="interrupted-retry-recovers-pending-shell",
        ),
        pytest.param(
            {"kind": "shell", "status": "retrying", "pending_attempt": 3},
            JobObservation(now=20.0, shell_liveness="absent"),
            JobTransition(
                updates={
                    "attempts": 3,
                    "status": "failed",
                    "updated_at": 20.0,
                    "completed_at": 20.0,
                    "exit_code": None,
                    "error": "retry was interrupted before a recoverable shell was committed",
                },
                remove_keys=(
                    "pending_attempt",
                    "pending_shell_id",
                    "pending_command_path",
                    "pending_log_path",
                    "pending_status_path",
                ),
                clear_operation=True,
            ),
            id="interrupted-retry-without-shell-fails",
        ),
        pytest.param(
            {"kind": "shell", "status": "stopping"},
            JobObservation(now=20.0, shell_liveness="live"),
            JobTransition(
                updates={
                    "status": "running",
                    "updated_at": 20.0,
                    "error": "recovered an interrupted stop request; shell is still active",
                },
                clear_operation=True,
            ),
            id="interrupted-stop-with-live-shell-recovers-running",
        ),
        pytest.param(
            {"kind": "shell", "status": "stopping"},
            JobObservation(now=20.0, shell_liveness="absent"),
            JobTransition(
                updates={
                    "status": "stopped",
                    "updated_at": 20.0,
                    "completed_at": 20.0,
                    "exit_code": None,
                    "error": None,
                },
                clear_operation=True,
            ),
            id="interrupted-stop-without-shell-completes",
        ),
        pytest.param(
            {"kind": "shell", "status": "running"},
            JobObservation(now=20.0, shell_liveness="live"),
            JobTransition(),
            id="running-live-shell-is-stable",
        ),
        pytest.param(
            {"kind": "shell", "status": "running"},
            JobObservation(now=20.0, shell_liveness="absent"),
            JobTransition(
                updates={
                    "status": "lost",
                    "updated_at": 20.0,
                    "completed_at": 20.0,
                    "exit_code": None,
                    "error": "job shell exited without a durable completion record",
                    "shell_absence_confirmed": True,
                }
            ),
            id="running-absent-shell-becomes-lost",
        ),
    ],
)
def test_reconcile_job_transition_table(
    job: dict[str, Any],
    observation: JobObservation,
    expected: JobTransition,
) -> None:
    assert reconcile_job(job, observation) == expected


def test_reconcile_job_never_mutates_input_row() -> None:
    job = {
        "kind": "shell",
        "status": "retrying",
        "pending_attempt": 2,
        "pending_shell_id": "shell-2",
    }
    original = dict(job)

    reconcile_job(job, JobObservation(now=20.0, shell_liveness="live"))

    assert job == original


@pytest.mark.parametrize(
    ("status", "operation"),
    [
        pytest.param("starting", "start", id="start"),
        pytest.param("stopping", "stop", id="stop"),
        pytest.param("retrying", "retry", id="retry"),
    ],
)
def test_refresh_active_shell_operation_skips_external_status_probes(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    operation: str,
) -> None:
    row: dict[str, Any] = {"kind": "shell", "status": status}

    monkeypatch.setattr(
        job_lifecycle,
        "_observed_active_operation",
        lambda _job: operation,
    )

    def fail_status_probe(_value: Any) -> JobStatusPayload | None:
        pytest.fail("active operation must short-circuit status observation")

    refreshed = job_lifecycle._refresh_job_status(
        row,
        set(),
        now=20.0,
        managed_job_has_local_task=lambda _job: pytest.fail(
            "shell reconciliation must not probe managed tasks"
        ),
        managed_job_liveness=lambda _job: pytest.fail(
            "shell reconciliation must not probe managed leases"
        ),
        read_status=fail_status_probe,
        read_status_path=fail_status_probe,
    )

    assert refreshed == row


def test_refresh_active_managed_operation_skips_live_state_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row: dict[str, Any] = {"kind": "managed", "status": "running"}
    monkeypatch.setattr(
        job_lifecycle,
        "_observed_active_operation",
        lambda _job: "retry",
    )

    refreshed = job_lifecycle._refresh_job_status(
        row,
        set(),
        now=20.0,
        managed_job_has_local_task=lambda _job: pytest.fail(
            "active managed operation must skip task probing"
        ),
        managed_job_liveness=lambda _job: pytest.fail(
            "active managed operation must skip lease probing"
        ),
        read_status=lambda _job: pytest.fail(
            "managed reconciliation must not read shell status"
        ),
        read_status_path=lambda _path: pytest.fail(
            "managed reconciliation must not read retry shell status"
        ),
    )

    assert refreshed == row
