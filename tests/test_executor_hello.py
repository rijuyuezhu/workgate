from pathlib import Path

from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.hello import build_executor_hello
from workgate.protocol.executor import (
    EXECUTOR_CAPABILITY_SESSIONS,
    JobInventorySummary,
    SessionInventorySummary,
    ShellInventorySummary,
)


def test_executor_hello_reports_complete_current_v1_namespace(
    tmp_path: Path,
) -> None:
    config = resolve_executor_config(Settings(workspace_root=tmp_path))

    hello = build_executor_hello(config)

    assert hello.protocol_version == 1
    assert hello.runtime.workgate_version
    assert hello.workspace_root == str(tmp_path.resolve(strict=False))
    assert hello.capabilities == (EXECUTOR_CAPABILITY_SESSIONS,)
    assert hello.sessions == ()
    assert hello.shells == ()
    assert hello.jobs == ()


def test_executor_hello_carries_supplied_resource_inventory(
    tmp_path: Path,
) -> None:
    config = resolve_executor_config(Settings(workspace_root=tmp_path))
    session_id = "sess_0000000000000000000001"
    sessions = (
        SessionInventorySummary(
            session_id=session_id,
            resolved_workdir=str(tmp_path),
            has_persistent_shells=True,
            has_active_jobs=True,
        ),
    )
    shells = (ShellInventorySummary(shell_id="shell-1", session_id=session_id),)
    jobs = (
        JobInventorySummary(
            job_id="job-1", session_id=session_id, status="running"
        ),
    )

    hello = build_executor_hello(
        config, sessions=sessions, shells=shells, jobs=jobs
    )

    assert hello.sessions == sessions
    assert hello.shells == shells
    assert hello.jobs == jobs


def test_executor_hello_builder_does_not_import_resource_authorities() -> None:
    source = Path("src/workgate/executor/hello.py").read_text(encoding="utf-8")

    assert "tool_session" not in source
    assert "remote_worker" not in source
    assert "executor.jobs" not in source
    assert "executor.shell" not in source
