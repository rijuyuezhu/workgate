import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_PATH = _REPO_ROOT / "scripts" / "testing" / "run-browser-e2e.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location(
        "workgate_browser_e2e_runner", _RUNNER_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(ModuleType, module)


def test_browser_e2e_runner_forwards_contract_and_exit_status(
    monkeypatch: Any, tmp_path: Path
) -> None:
    runner = _load_runner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run-browser-e2e.py", "--artifacts", "relative-artifacts"],
    )
    observed: dict[str, Any] = {}

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> SimpleNamespace:
        observed.update(command=command, env=env, check=check)
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.main() == 17
    assert observed["command"] == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-m",
        "browser",
        "tests/e2e/browser/test_human_ui_e2e.py",
    ]
    assert observed["check"] is False
    artifacts = tmp_path / "relative-artifacts"
    assert observed["env"]["WORKGATE_BROWSER_E2E"] == "1"
    assert observed["env"]["WORKGATE_BROWSER_ARTIFACTS"] == str(
        artifacts.resolve()
    )
    assert artifacts.is_dir()
