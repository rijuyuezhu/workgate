"""Bounded information retained in a failing HTTP E2E Job assertion."""

import json
from pathlib import Path

from tests.e2e.scenarios import _failed_job_evidence


def test_failed_job_assertion_shows_structured_runner_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-http"
    workspace.mkdir()
    state = tmp_path / "executor-state-http-1"
    state.mkdir()
    job_dir = state / "jobs"
    job_dir.mkdir()
    receipt = job_dir / "job_test-attempt-1.status.json"
    log = job_dir / "job_test-attempt-1.log"
    log.write_text("persisted-job-output\n")
    receipt.with_suffix(".runner-error.json").write_text(
        json.dumps(
            {
                "exception_type": "RuntimeError",
                "frames": [
                    {"module": "runner.py", "function": "main", "line": 42}
                ],
            }
        )
    )
    receipt.with_suffix(".runner-stderr.log").write_text(
        "legacy-sensitive-secret"
    )
    (state / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "job_id": "job_test",
                        "status": "lost",
                        "status_path": str(receipt),
                        "log_path": str(log),
                    }
                ]
            }
        )
    )
    process_logs = tmp_path / "process-logs-http"
    process_logs.mkdir()
    (process_logs / "executor-1.stderr.log").write_text("executor stderr")
    diagnostic = _failed_job_evidence(workspace, "job_test")
    assert "lost" in diagnostic
    assert "FileNotFoundError" in diagnostic
    assert "persisted-job-output" in diagnostic
    assert '"exception_type": "RuntimeError"' in diagnostic
    assert '"module": "runner.py"' in diagnostic
    assert "legacy-sensitive-secret" not in diagnostic
    assert "executor stderr" in diagnostic
