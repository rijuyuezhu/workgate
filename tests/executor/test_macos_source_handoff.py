"""A managed macOS source handoff is independent of the Executor LaunchAgent."""

import os
import sys
from pathlib import Path

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor import source_handoff
from workgate.executor.service import ExecutorServiceManager
from workgate.platform.host import HostOS

VERSION = "5.0.0a77"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_handoff, "host_os", lambda: HostOS.MACOS)
    for name in tuple(os.environ):
        if name.startswith("WORKGATE_"):
            monkeypatch.delenv(name)


@pytest.fixture
def prepared(tmp_path: Path):
    checkout = tmp_path / "managed source/versions" / ("a" * 40)
    python = checkout / ".venv/bin/python"
    script = checkout / "deploy/source/switch_macos.py"
    config = tmp_path / "executor config.yaml"
    for path in (python, script):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    config.write_text("profile: keep")
    stable = checkout.parent.parent / "bin/workgate"
    stable.parent.mkdir()
    stable.write_text("unchanged")
    return checkout, config, stable


def test_submit_detached_launchd_job(prepared, monkeypatch: pytest.MonkeyPatch):
    checkout, config, stable = prepared
    submitted = []
    monkeypatch.setattr(
        source_handoff,
        "run_bounded_capture",
        lambda command, **kwargs: (
            submitted.append((command, kwargs)) or (0, "")
        ),
    )
    source_handoff.schedule_macos_source_cutover(
        checkout,
        version=VERSION,
        config=config,
    )
    assert submitted == [
        (
            [
                "launchctl",
                "submit",
                "-l",
                "com.workgate.executor.source-cutover." + VERSION,
                "--",
                str(checkout / ".venv/bin/python"),
                str(checkout / "deploy/source/switch_macos.py"),
                "--tag",
                "v" + VERSION,
                "--install-root",
                str(checkout.parent.parent),
                "--executor-config",
                str(config),
            ],
            {"timeout": 30},
        )
    ]
    assert stable.read_text() == "unchanged"


@pytest.mark.parametrize(
    "fault",
    [
        "invalid-version",
        "missing-script",
        "symlink-script",
        "missing-python",
        "missing-config",
        "relative-config",
        "override",
        "wrong-host",
        "launchd-failure",
    ],
)
def test_invalid_input_cannot_schedule(
    prepared,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    tmp_path: Path,
):
    checkout, config, _stable = prepared
    version = VERSION
    if fault == "invalid-version":
        version = "../../bad"
    elif fault in {"missing-script", "symlink-script"}:
        path = checkout / "deploy/source/switch_macos.py"
        path.unlink()
        if fault == "symlink-script":
            other = tmp_path / "other.py"
            other.write_text("pass")
            path.symlink_to(other)
    elif fault == "missing-python":
        (checkout / ".venv/bin/python").unlink()
    elif fault == "missing-config":
        config.unlink()
    elif fault == "relative-config":
        config = Path("relative.yaml")
    elif fault == "override":
        monkeypatch.setenv("WORKGATE_LOG_LEVEL", "debug")
    elif fault == "wrong-host":
        monkeypatch.setattr(source_handoff, "host_os", lambda: HostOS.LINUX)

    commands = []
    monkeypatch.setattr(
        source_handoff,
        "run_bounded_capture",
        lambda command, **kwargs: (
            commands.append(command)
            or (1 if fault == "launchd-failure" else 0),
            "",
        ),
    )
    with pytest.raises((ValueError, RuntimeError)):
        source_handoff.schedule_macos_source_cutover(
            checkout,
            version=version,
            config=config,
        )
    assert commands == ([] if fault != "launchd-failure" else [commands[0]])


def test_manager_requires_verified_managed_source(
    prepared,
    monkeypatch: pytest.MonkeyPatch,
):
    checkout, config, _stable = prepared
    manager = ExecutorServiceManager(
        resolve_executor_config(
            Settings(default_workdir=checkout, state_dir=checkout / "state")
        ),
        system="Darwin",
    )
    monkeypatch.setattr(manager, "verified_source_checkout", lambda: None)
    with pytest.raises(RuntimeError, match="owned managed source"):
        manager.schedule_macos_source_cutover(VERSION)
    monkeypatch.setattr(manager, "verified_source_checkout", lambda: checkout)
    calls = []
    monkeypatch.setattr(
        source_handoff,
        "schedule_macos_source_cutover",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    manager.schedule_macos_source_cutover(VERSION)
    assert calls[0][0] == (checkout,)
    assert calls[0][1]["version"] == VERSION


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true",
    reason="isolated native launchctl submit smoke only on ephemeral macOS CI runner",
)
def test_native_launchctl_submit_executes_outside_caller(
    tmp_path: Path,
) -> None:
    """Exercise the actual launchctl CLI without touching a Workgate LaunchAgent."""
    import subprocess
    import time
    import uuid

    output = tmp_path / "started"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('ok')\n"
    )
    label = "com.workgate.test.source-cutover." + uuid.uuid4().hex
    submit = subprocess.run(
        [
            "launchctl",
            "submit",
            "-l",
            label,
            "--",
            sys.executable,
            str(script),
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert submit.returncode == 0, submit.stderr[-1000:]
    for _ in range(50):
        if output.exists() and output.read_text() == "ok":
            break
        time.sleep(0.1)
    assert output.read_text() == "ok"
