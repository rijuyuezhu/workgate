from pathlib import Path

from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.hello import build_executor_hello


def test_executor_hello_reports_complete_current_v1_namespace(
    tmp_path: Path,
) -> None:
    config = resolve_executor_config(Settings(workspace_root=tmp_path))

    hello = build_executor_hello(config)

    assert hello.protocol_version == 1
    assert hello.runtime.workgate_version
    assert hello.workspace_root == str(tmp_path.resolve(strict=False))
    assert hello.capabilities == ()
    assert hello.sessions == ()
    assert hello.shells == ()
    assert hello.jobs == ()


def test_executor_hello_does_not_import_legacy_resource_authorities() -> None:
    source = Path("src/workgate/executor/hello.py").read_text(encoding="utf-8")

    assert "tool_session" not in source
    assert "remote_worker" not in source
    assert "jobs" not in source.replace("jobs=()", "")
