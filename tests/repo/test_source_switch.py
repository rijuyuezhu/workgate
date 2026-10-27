"""Explicit Linux Control cutover, without touching live user services."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="explicit source service cutover is Linux-only"
)


@pytest.fixture
def switcher(monkeypatch):
    location = Path(__file__).resolve().parents[2] / "deploy" / "source"
    monkeypatch.syspath_prepend(str(location))
    spec = importlib.util.spec_from_file_location(
        "wg_source_switch", location / "switch.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(module.sys, "platform", "linux")
    return module


@pytest.fixture
def staged(tmp_path, switcher, monkeypatch):
    root = tmp_path / "managed source"
    origin = tmp_path / "origin"
    origin.mkdir()
    monkeypatch.setattr(switcher, "REPOSITORY", str(origin))

    def git(*args, cwd=origin):
        return subprocess.check_output(
            ["git", *args], cwd=cwd, text=True
        ).strip()

    git("init", "-q")
    git("config", "user.email", "test@invalid.example")
    git("config", "user.name", "Test")
    (origin / ".gitignore").write_text(".venv/\n")
    service = origin / "src/workgate/control/service.py"
    service.parent.mkdir(parents=True)
    service.write_text("# Test release with native Control service support\n")
    checkout = {}
    for version in ("v5.0.0a5", "v5.0.0a6"):
        (origin / "uv.lock").write_text("version = 1\n")
        (origin / "pyproject.toml").write_text(
            f"[project]\nname='workgate'\nversion='{version[1:]}'\n"
        )
        git("add", ".")
        git("commit", "-qm", version)
        git("tag", version)
        sha = git("rev-parse", "HEAD")
        target = root / "versions" / sha
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--branch",
                version,
                str(origin),
                str(target),
            ],
            check=True,
        )
        executable = switcher._python(target)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        checkout[version] = executable

    config = tmp_path / "control config.yaml"
    config.write_text("mode: http\nidentity: keep-me\n")
    unit = tmp_path / "systemd" / "user" / "workgate-control.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(
        "# Managed by workgate control install-service\n[Service]\n"
    )
    switcher._write_launcher(root, checkout["v5.0.0a5"])
    state = {
        "active_python": checkout["v5.0.0a5"],
        "load": "loaded",
        "active": "active",
        "fragment": str(unit),
        "error_new": False,
        "error_old": False,
        "calls": [],
        "role": "control",
        "executor_config": None,
    }
    original_run = switcher._run
    state["native_query"] = switcher._native
    state["native_install"] = switcher._install
    state["original_run"] = original_run

    def run(*args, cwd=None):
        if args[1:4] == ("-m", "workgate.main", "--version"):
            return "workgate " + (
                "5.0.0a6" if args[0] == str(checkout["v5.0.0a6"]) else "5.0.0a5"
            )
        return original_run(*args, cwd=cwd)

    def native(role):
        executable = state["active_python"]
        command = switcher._command(
            executable, role, config, state["executor_config"]
        )
        return {
            "LoadState": state["load"],
            "ActiveState": state["active"],
            "FragmentPath": state["fragment"],
            "ExecStart": (
                f"{{ path={command[0]} ; argv[]={' '.join(command)} ; ignore_errors=no ; "
                "start_time=[n/a] ; stop_time=[n/a] ; }}"
            ),
        }

    def install(executable, role, saved_config, saved_executor_config):
        assert role == state["role"]
        assert saved_config == config
        assert saved_executor_config == state["executor_config"]
        state["calls"].append(executable)
        state["active_python"] = executable
        if executable == checkout["v5.0.0a6"] and state["error_new"]:
            raise RuntimeError("new service failed")
        if executable == checkout["v5.0.0a5"] and state["error_old"]:
            raise RuntimeError("old recovery failed")

    monkeypatch.setattr(switcher, "_run", run)
    monkeypatch.setattr(switcher, "_native", native)
    monkeypatch.setattr(switcher, "_install", install)
    return root, config, checkout, state


def test_switch_uses_only_prepared_version_and_preserves_config(
    staged, switcher
):
    root, config, checkouts, state = staged
    before = config.read_bytes()
    switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == [checkouts["v5.0.0a6"]]
    assert switcher._old_python(root) == checkouts["v5.0.0a6"]
    assert config.read_bytes() == before
    assert checkouts["v5.0.0a5"].exists()
    # The same explicit command with the same verified binding is a no-op.
    switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == [checkouts["v5.0.0a6"]]
    # Switching to an already staged older tag is the manual rollback path.
    switcher.switch(root=root, tag="v5.0.0a5", control_config=config)
    assert state["calls"][-1] == checkouts["v5.0.0a5"]
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_control_rejects_older_tag_without_native_service_before_restart(
    staged, switcher, monkeypatch
):
    root, config, _, state = staged
    origin = Path(switcher.REPOSITORY)
    (origin / "src/workgate/control/service.py").unlink()
    (origin / "pyproject.toml").write_text(
        "[project]\nname='workgate'\nversion='5.0.0a4'\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "commit", "-qm", "legacy"], cwd=origin, check=True)
    subprocess.run(["git", "tag", "v5.0.0a4"], cwd=origin, check=True)
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=origin, text=True
    ).strip()
    checkout = root / "versions" / sha
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--branch",
            "v5.0.0a4",
            str(origin),
            str(checkout),
        ],
        check=True,
    )
    python = switcher._python(checkout)
    python.parent.mkdir(parents=True)
    python.touch()
    original_run = switcher._run

    def run(*args, cwd=None):
        if args[0] == str(python) and args[1:4] == (
            "-m",
            "workgate.main",
            "--version",
        ):
            return "workgate 5.0.0a4"
        return original_run(*args, cwd=cwd)

    monkeypatch.setattr(switcher, "_run", run)
    before = (root / "bin/workgate").read_bytes()

    with pytest.raises(RuntimeError, match="lacks native service"):
        switcher.switch(root=root, tag="v5.0.0a4", control_config=config)
    assert state["calls"] == []
    assert (root / "bin/workgate").read_bytes() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.update(load="not-found"),
        lambda state: state.update(active="inactive"),
        lambda state: state.update(fragment="/unexpected/control.service"),
        lambda state: state.update(active_python=Path("/unmanaged/python")),
    ],
)
def test_switch_rejects_uncertain_old_service(staged, switcher, mutate):
    root, config, checkouts, state = staged
    mutate(state)
    launcher = (root / "bin/workgate").read_bytes()
    with pytest.raises(RuntimeError, match="binding"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == []
    assert (root / "bin/workgate").read_bytes() == launcher


def test_switch_failure_restores_prior_running_service_and_launcher(
    staged, switcher
):
    root, config, checkouts, state = staged
    state["error_new"] = True
    before = (root / "bin/workgate").read_bytes()
    with pytest.raises(RuntimeError, match="previous service restored"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == [checkouts["v5.0.0a6"], checkouts["v5.0.0a5"]]
    assert state["active_python"] == checkouts["v5.0.0a5"]
    assert (root / "bin/workgate").read_bytes() == before


def test_switch_reports_failed_recovery_and_leaves_cli_old(staged, switcher):
    root, config, checkouts, state = staged
    state["error_new"] = state["error_old"] = True
    with pytest.raises(RuntimeError, match="recovery also failed"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_switch_refuses_non_linux(staged, switcher, monkeypatch):
    root, config, _, state = staged
    monkeypatch.setattr(switcher.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="Linux only"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == []


def test_switch_requires_prepared_tag_and_config(staged, switcher):
    root, config, _, state = staged
    with pytest.raises(RuntimeError, match="no prepared source release"):
        switcher.switch(root=root, tag="v5.0.0a7", control_config=config)
    with pytest.raises(RuntimeError, match="missing durable"):
        switcher.switch(
            root=root,
            tag="v5.0.0a6",
            control_config=config.with_name("missing.yaml"),
        )
    assert state["calls"] == []


@pytest.mark.parametrize("role", ["control", "colocate"])
def test_switch_real_subprocesses_with_isolated_fake_systemd(
    staged, switcher, monkeypatch, tmp_path, role
):
    """Exercise real Git validation, CLI subprocesses and systemctl invocation."""
    root, config, checkouts, state = staged
    executor_config = None
    if role == "colocate":
        executor_config = tmp_path / "executor.yaml"
        executor_config.write_text("state_dir: /paired/executor/state\n")
        unit = tmp_path / "systemd" / "user" / "workgate-colocate.service"
        unit.write_text("# Managed by workgate colocate install-service\n")
        state.update(
            role=role, executor_config=executor_config, fragment=str(unit)
        )
    fake_bin = tmp_path / "test-bin"
    fake_bin.mkdir()
    active = tmp_path / "simulated-active-python.txt"
    active.write_text(str(checkouts["v5.0.0a5"]))
    unit = Path(state["fragment"])
    monkeypatch.setenv("WG_SIM_ROLE", role)
    monkeypatch.setenv("WG_SIM_EXECUTOR_CONFIG", str(executor_config or ""))
    monkeypatch.setenv("WG_SIM_ACTIVE", str(active))
    monkeypatch.setenv("WG_SIM_UNIT", str(unit))
    monkeypatch.setenv("WG_SIM_CONTROL_CONFIG", str(config))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "exe = Path(os.environ['WG_SIM_ACTIVE']).read_text()\n"
        "cfg = os.environ['WG_SIM_CONTROL_CONFIG']\n"
        "role = os.environ['WG_SIM_ROLE']\n"
        "excfg = os.environ['WG_SIM_EXECUTOR_CONFIG']\n"
        "args = f'--control-config {cfg} --executor-config {excfg}' if role == 'colocate' else f'--config {cfg}'\n"
        "unit = os.environ['WG_SIM_UNIT']\n"
        "print('LoadState=loaded')\n"
        "print('ActiveState=active')\n"
        "print('FragmentPath=' + unit)\n"
        "print(f'ExecStart={{ path={exe} ; argv[]={exe} -m workgate.main {role} {args} ; ignore_errors=no ; pid=1 ; }}')\n"
    )
    systemctl.chmod(0o700)

    for tag, executable in checkouts.items():
        executable.write_text(
            "#!/bin/sh\n"
            'if [ "$3" = "--version" ]; then\n'
            f'  echo "workgate {tag[1:]}"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$3" = "$WG_SIM_ROLE" ] && [ "$4" = "install-service" ]; then\n'
            '  printf "%s" "$0" > "$WG_SIM_ACTIVE"\n'
            '  if [ "$WG_SIM_FAIL_NEW" = "$0" ]; then exit 33; fi\n'
            "  exit 0\n"
            "fi\n"
            "exit 73\n"
        )
        executable.chmod(0o700)

    monkeypatch.setattr(switcher, "_run", state["original_run"])
    monkeypatch.setattr(switcher, "_native", state["native_query"])
    monkeypatch.setattr(switcher, "_install", state["native_install"])
    # The original helpers now invoke real subprocesses in a temp-only PATH.
    switcher.switch(
        root=root,
        tag="v5.0.0a6",
        control_config=config,
        role=role,
        executor_config=executor_config,
    )
    assert active.read_text() == str(checkouts["v5.0.0a6"])
    assert switcher._old_python(root) == checkouts["v5.0.0a6"]

    switcher.switch(
        root=root,
        tag="v5.0.0a5",
        control_config=config,
        role=role,
        executor_config=executor_config,
    )
    assert active.read_text() == str(checkouts["v5.0.0a5"])

    monkeypatch.setenv("WG_SIM_FAIL_NEW", str(checkouts["v5.0.0a6"]))
    with pytest.raises(RuntimeError, match="previous service restored"):
        switcher.switch(
            root=root,
            tag="v5.0.0a6",
            control_config=config,
            role=role,
            executor_config=executor_config,
        )
    assert active.read_text() == str(checkouts["v5.0.0a5"])
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_switch_native_start_mismatch_recovers_old_version(
    staged, switcher, monkeypatch
):
    root, config, checkouts, state = staged
    native = switcher._native

    def misreport_new(role):
        response = native(role)
        if state["active_python"] == checkouts["v5.0.0a6"]:
            response["ExecStart"] = (
                "{ path=/unrelated/python ; argv[]=/unrelated/python ; ignore_errors=no ; }"
            )
        return response

    monkeypatch.setattr(switcher, "_native", misreport_new)
    with pytest.raises(RuntimeError, match="previous service restored"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == [checkouts["v5.0.0a6"], checkouts["v5.0.0a5"]]
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_switch_rejects_modified_staged_repo_before_service_change(
    staged, switcher
):
    root, config, checkouts, state = staged
    new_repo = checkouts["v5.0.0a6"].parents[2]
    (new_repo / "pyproject.toml").write_text("tampered")
    with pytest.raises(RuntimeError, match="modified staged release"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == []


def test_switch_rejects_untrusted_stage_remote_before_service_change(
    staged, switcher
):
    root, config, checkouts, state = staged
    repo = checkouts["v5.0.0a6"].parents[2]
    subprocess.run(
        [
            "git",
            "remote",
            "set-url",
            "origin",
            "https://unrelated.invalid/repo.git",
        ],
        cwd=repo,
        check=True,
    )
    with pytest.raises(RuntimeError, match="no prepared source release"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == []


def test_switch_rejects_missing_or_unmanaged_launcher(staged, switcher):
    root, config, _, state = staged
    launcher = root / "bin/workgate"
    launcher.unlink()
    with pytest.raises(RuntimeError, match="managed stable"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    launcher.write_text("#!/bin/sh\n# unmanaged\n/bin/true\n")
    with pytest.raises(RuntimeError, match="unmanaged launcher"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == []


@pytest.fixture
def colocate(staged, tmp_path):
    root, config, checkouts, state = staged
    executor_config = tmp_path / "executor config.yaml"
    executor_config.write_text("state_dir: /stable/pairing/identity\n")
    unit = tmp_path / "systemd" / "user" / "workgate-colocate.service"
    unit.write_text(
        "# Managed by workgate colocate install-service\n[Service]\n"
    )
    state["fragment"] = str(unit)
    state["role"] = "colocate"
    state["executor_config"] = executor_config
    return root, config, executor_config, checkouts, state


def test_colocate_switch_and_same_release_no_restart(colocate, switcher):
    root, control, executor, checkouts, state = colocate
    cfg_before = (control.read_bytes(), executor.read_bytes())
    switcher.switch(
        root=root,
        tag="v5.0.0a6",
        role="colocate",
        control_config=control,
        executor_config=executor,
    )
    assert state["calls"] == [checkouts["v5.0.0a6"]]
    assert switcher._old_python(root) == checkouts["v5.0.0a6"]
    switcher.switch(
        root=root,
        tag="v5.0.0a6",
        role="colocate",
        control_config=control,
        executor_config=executor,
    )
    assert state["calls"] == [checkouts["v5.0.0a6"]]
    switcher.switch(
        root=root,
        tag="v5.0.0a5",
        role="colocate",
        control_config=control,
        executor_config=executor,
    )
    assert state["calls"][-1] == checkouts["v5.0.0a5"]
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]
    assert (control.read_bytes(), executor.read_bytes()) == cfg_before


def test_colocate_failed_switch_recovers_previous_runtime(colocate, switcher):
    root, control, executor, checkouts, state = colocate
    state["error_new"] = True
    with pytest.raises(RuntimeError, match="previous service restored"):
        switcher.switch(
            root=root,
            tag="v5.0.0a6",
            role="colocate",
            control_config=control,
            executor_config=executor,
        )
    assert state["calls"] == [checkouts["v5.0.0a6"], checkouts["v5.0.0a5"]]
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_colocate_requires_exact_durable_paired_config(colocate, switcher):
    root, control, executor, _checkouts, state = colocate
    with pytest.raises(RuntimeError, match="existing durable Executor config"):
        switcher.switch(
            root=root, tag="v5.0.0a6", role="colocate", control_config=control
        )
    with pytest.raises(RuntimeError, match="existing durable Executor config"):
        switcher.switch(
            root=root,
            tag="v5.0.0a6",
            role="colocate",
            control_config=control,
            executor_config=executor.with_name("missing.yaml"),
        )
    assert state["calls"] == []


@pytest.mark.parametrize(
    "drift", ["wrong-fragment", "wrong-executor-config", "wrong-old-python"]
)
def test_colocate_refuses_unbound_native_service(
    colocate, switcher, monkeypatch, drift
):
    root, control, executor, _checkouts, state = colocate
    original_native = switcher._native

    def altered(role):
        native = original_native(role)
        if drift == "wrong-fragment":
            native["FragmentPath"] = "/unmanaged/service"
        elif drift == "wrong-executor-config":
            native["ExecStart"] = native["ExecStart"].replace(
                str(executor), "/other.yaml"
            )
        else:
            native["ExecStart"] = native["ExecStart"].replace(
                "argv[]=", "argv[]=/old/python "
            )
        return native

    monkeypatch.setattr(switcher, "_native", altered)
    with pytest.raises(RuntimeError, match="managed version"):
        switcher.switch(
            root=root,
            tag="v5.0.0a6",
            role="colocate",
            control_config=control,
            executor_config=executor,
        )
    assert state["calls"] == []


def test_colocate_rejects_control_unit_even_if_loaded(colocate, switcher):
    root, control, executor, _checkouts, state = colocate
    unit = Path(state["fragment"])
    unit.write_text("# Managed by workgate control install-service\n")
    with pytest.raises(RuntimeError, match="managed version"):
        switcher.switch(
            root=root,
            tag="v5.0.0a6",
            role="colocate",
            control_config=control,
            executor_config=executor,
        )
    assert state["calls"] == []


def test_colocate_installer_invokes_existing_native_service_cli(
    colocate, switcher, monkeypatch
):
    _root, control, executor, checkouts, state = colocate
    calls = []

    def capture(*args, cwd=None):
        calls.append((args, cwd))
        return ""

    monkeypatch.setattr(switcher, "_run", capture)
    monkeypatch.setattr(switcher, "_install", state["native_install"])
    python = checkouts["v5.0.0a6"]
    switcher._install(python, "colocate", control, executor)
    assert calls == [
        (
            (
                str(python),
                "-m",
                "workgate.main",
                "colocate",
                "install-service",
                "--control-config",
                str(control),
                "--executor-config",
                str(executor),
            ),
            python.parents[2],
        )
    ]


def test_colocate_rejects_missing_config_and_unsupported_roles(
    colocate, switcher
):
    root, control, executor, _checkouts, state = colocate
    with pytest.raises(
        ValueError, match="does not apply to standalone Executor"
    ):
        switcher.switch(
            root=root,
            tag="v5.0.0a6",
            role="executor",
            control_config=control,
            executor_config=executor,
        )
    assert state["calls"] == []


@pytest.fixture
def executor_switch(staged, switcher, monkeypatch, tmp_path):
    root, _control, checkouts, _state = staged
    config = tmp_path / "durable executor.yaml"
    config.write_text("state_dir: /paired/executor/state\n")
    state_dir = tmp_path / "app-state"
    service_dir = state_dir / "executor" / "service"
    service_dir.mkdir(parents=True)
    (service_dir / "launcher.py").write_text(
        "# Existing managed source launcher\n"
    )
    profile = state_dir / "executor" / "profile.json"
    profile.write_bytes(b'{"executor_id":"kept","credential":"keep-secret"}')
    unit = tmp_path / "systemd" / "user" / "workgate-executor.service"
    unit.write_text("[Unit]\nDescription=Workgate executor\n")
    state = {
        "active": checkouts["v5.0.0a5"],
        "unit": unit,
        "service_dir": service_dir,
        "profile": profile,
        "error_new": False,
        "error_old": False,
        "tamper_profile": False,
        "calls": [],
    }
    original_run = switcher._run
    original_native = switcher._native
    original_executor_status = switcher._executor_status
    state["original_run"] = original_run
    state["original_native"] = original_native
    state["original_executor_status"] = original_executor_status

    def run(*args: str, cwd: Path | None = None) -> str:
        executable = args[0] if args else ""
        if len(args) > 4 and args[1:5] == (
            "-m",
            "workgate.main",
            "executor",
            "install-service",
        ):
            python = Path(executable)
            state["calls"].append(python)
            state["active"] = python
            if state["tamper_profile"] and python == checkouts["v5.0.0a6"]:
                profile.write_bytes(b"tampered")
            if python == checkouts["v5.0.0a6"] and state["error_new"]:
                raise RuntimeError("simulated failed Executor install")
            if python == checkouts["v5.0.0a5"] and state["error_old"]:
                raise RuntimeError("simulated failed Executor recovery")
            return ""
        if args[1:4] == ("-m", "workgate.main", "--version"):
            return "workgate " + (
                "5.0.0a6"
                if executable == str(checkouts["v5.0.0a6"])
                else "5.0.0a5"
            )
        return original_run(*args, cwd=cwd)

    def status(python, yaml):
        assert yaml == config
        return {
            "State": "running",
            "Backend": "systemd",
            "Service": str(unit),
            "Log": str(service_dir / "executor.log"),
            "Runtime owner": "source",
            "Runtime": "current" if python == state["active"] else "stale",
        }

    def native(role):
        assert role == "executor"
        python = state["active"]
        command = [str(python), str((service_dir / "launcher.py").resolve())]
        return {
            "LoadState": "loaded",
            "ActiveState": "active",
            "FragmentPath": str(unit),
            "ExecStart": (
                f"{{ path={python} ; argv[]={' '.join(command)} ; "
                "ignore_errors=no ; pid=1 ; }}"
            ),
        }

    monkeypatch.setattr(switcher, "_run", run)
    monkeypatch.setattr(switcher, "_native", native)
    monkeypatch.setattr(switcher, "_executor_status", status)
    return root, config, checkouts, state


def test_executor_switch_and_explicit_rollback_preserve_paired_identity(
    executor_switch, switcher
):
    root, config, checkouts, state = executor_switch
    identity = state["profile"].read_bytes()
    switcher.switch(
        root=root, tag="v5.0.0a6", role="executor", executor_config=config
    )
    assert state["calls"] == [checkouts["v5.0.0a6"]]
    assert switcher._old_python(root) == checkouts["v5.0.0a6"]
    assert state["profile"].read_bytes() == identity
    switcher.switch(
        root=root, tag="v5.0.0a6", role="executor", executor_config=config
    )
    assert len(state["calls"]) == 1
    switcher.switch(
        root=root, tag="v5.0.0a5", role="executor", executor_config=config
    )
    assert state["calls"][-1] == checkouts["v5.0.0a5"]
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]
    assert state["profile"].read_bytes() == identity


def test_executor_native_activation_failure_restores_old_and_keeps_identity(
    executor_switch, switcher
):
    root, config, checkouts, state = executor_switch
    state["error_new"] = True
    before = state["profile"].read_bytes()
    with pytest.raises(RuntimeError, match="previous service restored"):
        switcher.switch(
            root=root, tag="v5.0.0a6", role="executor", executor_config=config
        )
    assert state["calls"] == [checkouts["v5.0.0a6"], checkouts["v5.0.0a5"]]
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]
    assert state["profile"].read_bytes() == before


def test_executor_failing_recovery_reports_error(executor_switch, switcher):
    root, config, checkouts, state = executor_switch
    state["error_new"] = state["error_old"] = True
    with pytest.raises(RuntimeError, match="recovery also failed"):
        switcher.switch(
            root=root, tag="v5.0.0a6", role="executor", executor_config=config
        )
    assert state["calls"] == [checkouts["v5.0.0a6"], checkouts["v5.0.0a5"]]
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_executor_detects_pairing_identity_mutation_and_never_claims_recovery(
    executor_switch, switcher
):
    root, config, checkouts, state = executor_switch
    state["tamper_profile"] = True
    with pytest.raises(RuntimeError, match="recovery also failed"):
        switcher.switch(
            root=root, tag="v5.0.0a6", role="executor", executor_config=config
        )
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


@pytest.mark.parametrize(
    "case", ["wrong-unit", "wrong-python", "stale-metadata", "wrong-profile"]
)
def test_executor_rejects_untrusted_running_native_binding(
    executor_switch, switcher, monkeypatch, case
):
    root, config, checkouts, state = executor_switch
    if case == "wrong-unit":
        native = switcher._native

        def wrong_unit(role):
            result = native(role)
            result["FragmentPath"] = "/foreign/service"
            return result

        monkeypatch.setattr(switcher, "_native", wrong_unit)
    elif case == "wrong-python":
        native = switcher._native

        def wrong_command(role):
            result = native(role)
            result["ExecStart"] = result["ExecStart"].replace(
                "argv[]=", "argv[]=/foreign/python "
            )
            return result

        monkeypatch.setattr(switcher, "_native", wrong_command)
    elif case == "stale-metadata":
        status = switcher._executor_status

        def stale_metadata(python, cfg):
            result = status(python, cfg)
            if python == checkouts["v5.0.0a5"]:
                result["Runtime"] = "stale"
            return result

        monkeypatch.setattr(switcher, "_executor_status", stale_metadata)
    else:
        state["profile"].unlink()
    with pytest.raises(RuntimeError):
        switcher.switch(
            root=root, tag="v5.0.0a6", role="executor", executor_config=config
        )
    assert state["calls"] == []
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_executor_requires_existing_yaml_and_rejects_control_config(
    executor_switch, switcher
):
    root, config, _checkouts, state = executor_switch
    with pytest.raises(RuntimeError, match="durable Executor YAML"):
        switcher.switch(root=root, tag="v5.0.0a6", role="executor")
    with pytest.raises(ValueError, match="Control config"):
        switcher.switch(
            root=root,
            tag="v5.0.0a6",
            role="executor",
            control_config=config,
            executor_config=config,
        )
    assert state["calls"] == []


@pytest.mark.parametrize(
    "drift", ["different-log", "different-unit", "not-running"]
)
def test_executor_new_cli_cannot_change_persistent_identity_namespace(
    executor_switch, switcher, monkeypatch, drift
):
    root, config, checkouts, state = executor_switch
    existing = switcher._executor_status

    def new_status(python, yaml):
        result = existing(python, yaml)
        if python == checkouts["v5.0.0a6"]:
            if drift == "different-log":
                result["Log"] = "/another/state/executor/service/executor.log"
            elif drift == "different-unit":
                result["Service"] = (
                    "/another/systemd/user/workgate-executor.service"
                )
            else:
                result["State"] = "stopped"
        return result

    monkeypatch.setattr(switcher, "_executor_status", new_status)
    with pytest.raises(
        RuntimeError, match="different service or state directory"
    ):
        switcher.switch(
            root=root, tag="v5.0.0a6", role="executor", executor_config=config
        )
    assert not state["calls"]


def test_executor_new_native_binding_mismatch_rolls_back(
    executor_switch, switcher, monkeypatch
):
    root, config, checkouts, state = executor_switch
    old = switcher._native

    def mismatched(role):
        result = old(role)
        if state["active"] == checkouts["v5.0.0a6"]:
            result["ExecStart"] = result["ExecStart"].replace(
                "argv[]=", "argv[]=/unmanaged/python "
            )
        return result

    monkeypatch.setattr(switcher, "_native", mismatched)
    with pytest.raises(RuntimeError, match="previous service restored"):
        switcher.switch(
            root=root, tag="v5.0.0a6", role="executor", executor_config=config
        )
    assert state["calls"] == [checkouts["v5.0.0a6"], checkouts["v5.0.0a5"]]
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_executor_full_subprocess_cutover_and_failure_recovery(
    executor_switch, switcher, monkeypatch, tmp_path
):
    """Git checkout + real CLI/systemd subprocess path; no pairing actions."""
    root, config, checkouts, state = executor_switch
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    active = tmp_path / "native-executor-python.txt"
    actions = tmp_path / "actions.txt"
    active.write_text(str(checkouts["v5.0.0a5"]))
    monkeypatch.setenv("WG_SIM_ACTIVE", str(active))
    monkeypatch.setenv("WG_SIM_ACTIONS", str(actions))
    monkeypatch.setenv("WG_SIM_UNIT", str(state["unit"]))
    monkeypatch.setenv("WG_SIM_LOG", str(state["service_dir"] / "executor.log"))
    monkeypatch.setenv(
        "WG_SIM_LAUNCHER", str(state["service_dir"] / "launcher.py")
    )
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "exe = Path(os.environ['WG_SIM_ACTIVE']).read_text()\n"
        "launch = os.environ['WG_SIM_LAUNCHER']\n"
        "print('LoadState=loaded')\n"
        "print('ActiveState=active')\n"
        "print('FragmentPath=' + os.environ['WG_SIM_UNIT'])\n"
        "print(f'ExecStart={{ path={exe} ; argv[]={exe} {launch} ; ignore_errors=no ; pid=1 ; }}')\n"
    )
    systemctl.chmod(0o700)
    for tag, executable in checkouts.items():
        executable.write_text(
            "#!/bin/sh\n"
            'if [ "$3" = "--version" ]; then\n'
            f'  echo "workgate {tag[1:]}"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$3" = "executor" ] && [ "$4" = "status" ]; then\n'
            '  echo "State: running"\n'
            '  echo "Backend: systemd"\n'
            '  echo "Service: $WG_SIM_UNIT"\n'
            '  echo "Log: $WG_SIM_LOG"\n'
            '  echo "Runtime owner: source"\n'
            '  if [ "$(cat "$WG_SIM_ACTIVE")" = "$0" ]; then echo "Runtime: current"; else echo "Runtime: stale"; fi\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$3" = "executor" ] && [ "$4" = "install-service" ]; then\n'
            '  echo "install:$0" >> "$WG_SIM_ACTIONS"\n'
            '  printf "%s" "$0" > "$WG_SIM_ACTIVE"\n'
            '  if [ "$WG_SIM_FAIL_NEW" = "$0" ]; then exit 27; fi\n'
            "  exit 0\n"
            "fi\n"
            'echo "unexpected Executor action" >&2\n'
            "exit 73\n"
        )
        executable.chmod(0o700)
    monkeypatch.setattr(switcher, "_run", state["original_run"])

    def native_executor(role):
        output = switcher._run(
            "systemctl",
            "--user",
            "show",
            f"workgate-{role}.service",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=FragmentPath",
            "--property=ExecStart",
        )
        return dict(
            line.split("=", 1) for line in output.splitlines() if "=" in line
        )

    monkeypatch.setattr(switcher, "_native", native_executor)
    monkeypatch.setattr(
        switcher, "_executor_status", state["original_executor_status"]
    )

    identity_before = state["profile"].read_bytes()
    switcher.switch(
        root=root, tag="v5.0.0a6", role="executor", executor_config=config
    )
    assert active.read_text() == str(checkouts["v5.0.0a6"])
    assert state["profile"].read_bytes() == identity_before
    assert switcher._old_python(root) == checkouts["v5.0.0a6"]

    switcher.switch(
        root=root, tag="v5.0.0a5", role="executor", executor_config=config
    )
    assert active.read_text() == str(checkouts["v5.0.0a5"])

    monkeypatch.setenv("WG_SIM_FAIL_NEW", str(checkouts["v5.0.0a6"]))
    with pytest.raises(RuntimeError, match="previous service restored"):
        switcher.switch(
            root=root, tag="v5.0.0a6", role="executor", executor_config=config
        )
    assert active.read_text() == str(checkouts["v5.0.0a5"])
    assert state["profile"].read_bytes() == identity_before
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]
    assert "connect" not in actions.read_text()


@pytest.mark.parametrize("tamper", ["origin", "head", "tag"])
def test_staged_release_identity_tampering_refused_before_native_cutover(
    staged, switcher, tmp_path, tamper
):
    root, config, checkouts, state = staged
    checkout = checkouts["v5.0.0a6"].parents[2]
    if tamper == "origin":
        subprocess.run(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                str(tmp_path / "wrong-origin"),
            ],
            cwd=checkout,
            check=True,
        )
    elif tamper == "head":
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@invalid.example",
                "commit",
                "--allow-empty",
                "-qm",
                "unexpected HEAD",
            ],
            cwd=checkout,
            check=True,
        )
    else:
        subprocess.run(
            ["git", "tag", "-d", "v5.0.0a6"],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
    launcher = (root / "bin/workgate").read_bytes()
    with pytest.raises(RuntimeError, match="no prepared source release"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert state["calls"] == []
    assert (root / "bin/workgate").read_bytes() == launcher
