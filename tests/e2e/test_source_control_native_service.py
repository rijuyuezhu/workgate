"""Real Control CLI native lifecycle against a disposable systemctl stand-in.

The stand-in responds to systemd --user queries but never starts any real
system service. This checks CLI-to-unit wiring without touching host units.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.e2e.helpers import (
    isolated_process_env,
    isolated_xdg_env,
    provision_executor_pair,
)
from workgate.persistence import FileStateStore

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux systemd user units only"
)

_SYSTEMCTL = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["WG_TEST_NATIVE_ROOT"])
unit_dir = root / "systemd" / "user"
unit = unit_dir / "workgate-control.service"
state = root / "active"
colocate = unit_dir / "workgate-colocate.service"
colocate_state = root / "colocate-active"
argv = sys.argv[1:]
assert argv.pop(0) == "--user"
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(argv) + "\\n")
if argv == ["show", "--property=UnitPath", "--value"]:
    print(f'"{unit_dir}" /etc/systemd/user')
elif argv == ["show", "workgate-control.service", "--property=LoadState", "--value"]:
    print("loaded" if unit.is_file() else "not-found")
elif argv == ["show", "workgate-colocate.service", "--property=LoadState", "--value"]:
    print("loaded" if colocate.is_file() else "not-found")
elif argv == ["is-active", "workgate-colocate.service"]:
    print(colocate_state.read_text() if colocate_state.exists() else "inactive")
elif argv in (
    ["enable", "workgate-colocate.service"],
    ["restart", "workgate-colocate.service"],
    ["start", "workgate-colocate.service"],
):
    colocate_state.write_text("active")
elif argv in (
    ["stop", "workgate-colocate.service"],
    ["disable", "--now", "workgate-colocate.service"],
):
    colocate_state.write_text("inactive")
elif argv == ["show", "workgate-control.service",
              "--property=LoadState", "--property=ActiveState",
              "--property=FragmentPath", "--property=ExecStart"]:
    import shlex
    values = shlex.split(
        next(line.partition("=")[2] for line in unit.read_text().splitlines()
             if line.startswith("ExecStart="))
    ) if unit.is_file() else []
    print("LoadState=" + ("loaded" if unit.is_file() else "not-found"))
    print("ActiveState=" + (state.read_text() if state.exists() else "inactive"))
    print("FragmentPath=" + (str(unit) if unit.is_file() else ""))
    print("ExecStart=" + (
        f"{{ path={values[0]} ; argv[]={' '.join(values)} ; ignore_errors=no ; }}"
        if values else ""
    ))
elif argv == ["show-environment"]:
    pass
elif argv == ["is-active", "workgate-control.service"]:
    print(state.read_text() if state.exists() else "inactive")
elif argv in (
    ["daemon-reload"],
    ["enable", "workgate-control.service"],
):
    pass
elif argv in (
    ["start", "workgate-control.service"],
    ["restart", "workgate-control.service"],
):
    state.write_text("active")
elif argv in (
    ["stop", "workgate-control.service"],
    ["disable", "--now", "workgate-control.service"],
):
    state.write_text("inactive")
else:
    raise SystemExit(f"Unexpected systemctl action: {argv}")
"""


@pytest.fixture
def isolated_native_control(tmp_path: Path):
    unit_dir = tmp_path / "native" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "systemctl"
    shim.write_text(_SYSTEMCTL)
    shim.chmod(0o700)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(mode=0o700)
    config = cfg_dir / "control.yaml"
    env = isolated_xdg_env(tmp_path / "xdg")
    env["HOME"] = str(tmp_path)
    env["WG_TEST_NATIVE_ROOT"] = str(tmp_path / "native")
    env["PATH"] = str(shim_dir) + os.pathsep + os.environ.get("PATH", "")
    child_env = isolated_process_env(env)

    def run(
        *command: str, success: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "workgate.main", "control", *command],
            env=child_env,
            text=True,
            capture_output=True,
            timeout=35,
        )
        if success:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
        return result

    run("init-config", "--config", str(config))
    return (
        run,
        config,
        unit_dir / "workgate-control.service",
        tmp_path / "native",
        child_env,
    )


def test_real_control_cli_install_status_stop_start_and_uninstall_preserves_identity(
    isolated_native_control,
):
    run, config, unit, native, _env = isolated_native_control
    identity = config.read_bytes()
    assert yaml.safe_load(identity)["oauth_admin_pin"]
    assert "not-installed" in run("status-service").stdout

    run("install-service", "--config", str(config))
    assert unit.is_file()
    unit_text = unit.read_text()
    assert unit_text.startswith("# Managed by workgate control install-service")
    assert "UMask=0077" in unit_text
    assert "UnsetEnvironment=" in unit_text
    assert str(config) in unit_text
    assert str(Path(sys.executable).absolute()) in unit_text
    assert unit.stat().st_mode & 0o077 == 0
    assert "active" in run("status-service").stdout
    run("stop-service")
    assert "inactive" in run("status-service").stdout
    run("start-service")
    assert "active" in run("status-service").stdout
    run("restart-service")

    # Service lifecycle must preserve the real generated OAuth owner identity.
    assert config.read_bytes() == identity
    run("uninstall-service")
    assert not unit.exists()
    assert "not-installed" in run("status-service").stdout
    assert config.read_bytes() == identity
    calls = [
        json.loads(line)
        for line in (native / "calls.jsonl").read_text().splitlines()
    ]
    assert ["restart", "workgate-control.service"] in calls
    assert ["disable", "--now", "workgate-control.service"] in calls


def test_real_control_cli_refuses_foreign_native_unit_without_overwriting(
    isolated_native_control,
):
    run, config, unit, native, _env = isolated_native_control
    unit.write_text("[Service]\nExecStart=/usr/bin/false\n")
    before = unit.read_bytes()
    identity = config.read_bytes()
    failure = run("install-service", "--config", str(config), success=False)
    assert "unmanaged service" in failure.stderr
    failure = run("uninstall-service", success=False)
    assert "unmanaged service" in failure.stderr
    assert unit.read_bytes() == before
    assert config.read_bytes() == identity
    calls = [
        json.loads(line)
        for line in (native / "calls.jsonl").read_text().splitlines()
    ]
    assert not any(
        action[0] in ("restart", "disable", "enable") for action in calls
    )


def test_full_control_installer_orchestrates_real_native_cli_and_safe_rerun(
    isolated_native_control, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Only the expensive pinned release preparation is stubbed."""
    import importlib

    run, config, unit, native, env = isolated_native_control
    # The standalone installer is not a package; import from the source folder.
    location = Path(__file__).resolve().parents[2] / "deploy/source"
    monkeypatch.syspath_prepend(str(location))
    installer = importlib.import_module("install")
    source_switch = importlib.import_module("switch")
    root = tmp_path / "install-root"
    sha = "b" * 40
    checkout = root / "versions" / sha
    python = checkout / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    # Reuse the existing test venv's dependencies without syncing or mutating it.
    venv = Path(sys.prefix)
    (checkout / ".venv/pyvenv.cfg").write_bytes(
        (venv / "pyvenv.cfg").read_bytes()
    )
    (checkout / ".venv/lib").symlink_to(venv / "lib", target_is_directory=True)
    python.symlink_to(sys.executable)
    monkeypatch.setattr(installer, "prepare", lambda **kwargs: checkout)
    monkeypatch.setattr(
        source_switch, "_staged_python", lambda _root, _tag: python
    )
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        if key in (
            "HOME",
            "PATH",
            "PYTHONPATH",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "WG_TEST_NATIVE_ROOT",
        ):
            monkeypatch.setenv(key, value)
    before = config.read_bytes()
    options = dict(
        role="control",
        tag="v5.0.0a11",
        root=root,
        expected_commit=sha,
        control_config=config,
    )
    try:
        assert installer.install(**options) == checkout
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"source CLI failed: {exc.stderr}")
    assert unit.is_file()
    assert "active" in run("status-service").stdout
    assert config.read_bytes() == before
    receipt = json.loads((root / "install.json").read_text())
    assert receipt["role"] == "control"
    assert receipt["tag"] == "v5.0.0a11"
    assert (root / "install.json").stat().st_mode & 0o077 == 0
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kwargs: pytest.fail("same release must not run uv sync"),
    )
    assert installer.install(**options) == checkout
    calls = [
        json.loads(line)
        for line in (native / "calls.jsonl").read_text().splitlines()
    ]
    assert calls.count(["restart", "workgate-control.service"]) == 1
    assert config.read_bytes() == before

    # A stopped native unit is not a successful same-release re-install.
    run("stop-service")
    receipt_before = (root / "install.json").read_bytes()
    with pytest.raises(RuntimeError, match="already installed"):
        installer.install(**options)
    assert (root / "install.json").read_bytes() == receipt_before
    assert config.read_bytes() == before


@pytest.fixture
def colocate_env(isolated_native_control, tmp_path: Path):
    """Real CLI, disposable native backend and a pairing option."""
    _run, control, _control_unit, native, env = isolated_native_control
    executor_state = tmp_path / "paired-executor"
    executor_yaml = tmp_path / "executor.yaml"
    executor_yaml.write_text(f"state_dir: {executor_state}\n")
    unit = native / "systemd/user/workgate-colocate.service"
    profile = FileStateStore(
        lambda: executor_state
    ).layout.executor_profile_path

    def run(*args: str, success: bool = True):
        result = subprocess.run(
            [sys.executable, "-m", "workgate.main", "colocate", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=35,
        )
        if success:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
        return result

    def pair():
        provision_executor_pair(
            control_state_dir=tmp_path / "trust-state",
            executor_state_dir=executor_state,
            control_url="http://127.0.0.1:8765",
        )
        return profile.read_bytes()

    args = (
        "install-service",
        "--control-config",
        str(control),
        "--executor-config",
        str(executor_yaml),
    )
    return run, pair, args, unit, profile, native


def test_real_colocate_cli_requires_pairing_then_preserves_identity_through_lifecycle(
    colocate_env,
):
    run, pair, args, unit, profile, native = colocate_env
    failed = run(*args, success=False)
    assert "pair the executor" in failed.stderr
    assert not unit.exists()
    identity = pair()
    assert "not-installed" in run("status-service").stdout

    run(*args)
    content = unit.read_text()
    assert content.startswith("# Managed by workgate colocate install-service")
    assert args[2] in content and args[4] in content
    assert "UMask=0077" in content
    assert unit.stat().st_mode & 0o077 == 0
    assert "active" in run("status-service").stdout
    run("stop-service")
    assert "inactive" in run("status-service").stdout
    run("start-service")
    run("restart-service")
    assert profile.read_bytes() == identity
    run("uninstall-service")
    assert not unit.exists()
    assert "not-installed" in run("status-service").stdout
    assert profile.read_bytes() == identity
    commands = [
        json.loads(line)
        for line in (native / "calls.jsonl").read_text().splitlines()
    ]
    assert ["disable", "--now", "workgate-colocate.service"] in commands


def test_real_colocate_cli_does_not_overwrite_foreign_unit_or_pairing(
    colocate_env,
):
    run, pair, args, unit, profile, native = colocate_env
    identity = pair()
    foreign = "[Service]\nExecStart=/bin/false\n"
    unit.write_text(foreign)
    assert "unmanaged service" in run(*args, success=False).stderr
    assert "unmanaged service" in run("uninstall-service", success=False).stderr
    assert unit.read_text() == foreign
    assert profile.read_bytes() == identity
    commands = [
        json.loads(line)
        for line in (native / "calls.jsonl").read_text().splitlines()
    ]
    assert not any(
        item[0] in {"enable", "restart", "disable"}
        and item[-1] == "workgate-colocate.service"
        for item in commands
    )
