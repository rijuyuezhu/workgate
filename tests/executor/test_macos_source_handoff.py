"""A managed macOS source handoff is independent of the Executor LaunchAgent."""

import os
import plistlib
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


def test_one_shot_launchd_agent_never_restarts(
    prepared, monkeypatch: pytest.MonkeyPatch
):
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
        checkout, version=VERSION, config=config
    )
    directory = checkout.parent.parent / "source-cutover"
    plist = directory / (VERSION + ".plist")
    payload = plistlib.loads(plist.read_bytes())
    assert payload == {
        "Label": "com.workgate.executor.source-cutover." + VERSION,
        "ProgramArguments": [
            str(checkout / ".venv/bin/python"),
            str(checkout / "deploy/source/switch_macos.py"),
            "--tag",
            "v" + VERSION,
            "--install-root",
            str(checkout.parent.parent),
            "--executor-config",
            str(config),
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(directory / "cutover.log"),
        "StandardErrorPath": str(directory / "cutover.log"),
    }
    assert submitted == [
        (
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
            {"timeout": 30},
        )
    ]
    assert stable.read_text() == "unchanged"
    assert directory.stat().st_mode & 0o077 == 0
    assert plist.stat().st_mode & 0o077 == 0

    # It is critical that one target cannot be scheduled twice.
    with pytest.raises(FileExistsError):
        source_handoff.schedule_macos_source_cutover(
            checkout,
            version=VERSION,
            config=config,
        )
    assert len(submitted) == 1


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


def test_cutover_directory_symlink_is_rejected(
    prepared, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    checkout, config, _stable = prepared
    directory = checkout.parent.parent / "source-cutover"
    other = tmp_path / "foreign"
    other.mkdir()
    directory.symlink_to(other, target_is_directory=True)
    calls = []
    monkeypatch.setattr(
        source_handoff,
        "run_bounded_capture",
        lambda command, **kwargs: calls.append(command) or (0, ""),
    )
    with pytest.raises(OSError):
        source_handoff.schedule_macos_source_cutover(
            checkout, version=VERSION, config=config
        )
    assert not calls
    assert not list(other.iterdir())


def test_uncertain_launchd_handoff_retains_duplicate_guard(
    prepared,
    monkeypatch: pytest.MonkeyPatch,
):
    checkout, config, _stable = prepared
    calls = []
    monkeypatch.setattr(
        source_handoff,
        "run_bounded_capture",
        lambda command, **kwargs: calls.append(command) or (1, ""),
    )
    with pytest.raises(RuntimeError, match="could not schedule"):
        source_handoff.schedule_macos_source_cutover(
            checkout, version=VERSION, config=config
        )
    assert (
        checkout.parent.parent / "source-cutover" / (VERSION + ".plist")
    ).is_file()
    with pytest.raises(FileExistsError):
        source_handoff.schedule_macos_source_cutover(
            checkout, version=VERSION, config=config
        )
    assert len(calls) == 1


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
    reason="isolated native launchctl bootstrap smoke only on ephemeral macOS CI",
)
def test_native_one_shot_launchd_agent_exits_without_restart(
    tmp_path: Path,
) -> None:
    """Actually bootstrap an isolated KeepAlive=False LaunchAgent on macOS."""
    import subprocess
    import time
    import uuid

    output = tmp_path / "runs.txt"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\\nimport sys\\np = Path(sys.argv[1])\\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\\n"
    )
    label = "com.workgate.test.source-cutover." + uuid.uuid4().hex
    plist = tmp_path / "runner.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [sys.executable, str(script), str(output)],
                "RunAtLoad": True,
                "KeepAlive": False,
            }
        )
    )
    domain = f"gui/{os.getuid()}"
    submit = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        assert submit.returncode == 0, submit.stderr[-1000:]
        for _ in range(50):
            if output.exists():
                break
            time.sleep(0.1)
        assert output.read_text() == "x"
        loaded = subprocess.run(
            ["launchctl", "print", domain + "/" + label],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        # The production switch verifies these loaded native-action fields,
        # not just the saved plist on disk.
        assert f"path = {plist}" in loaded.stdout
        assert f"program = {sys.executable}" in loaded.stdout
        time.sleep(1)
        assert output.read_text() == "x"
    finally:
        subprocess.run(
            ["launchctl", "bootout", domain + "/" + label],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
