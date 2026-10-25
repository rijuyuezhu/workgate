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

    def native():
        executable = state["active_python"]
        command = [
            str(executable),
            "-m",
            "workgate.main",
            "control",
            "--config",
            str(config),
        ]
        return {
            "LoadState": state["load"],
            "ActiveState": state["active"],
            "FragmentPath": state["fragment"],
            "ExecStart": (
                f"{{ path={command[0]} ; argv[]={' '.join(command)} ; ignore_errors=no ; "
                "start_time=[n/a] ; stop_time=[n/a] ; }}"
            ),
        }

    def install(executable, saved_config):
        assert saved_config == config
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


def test_switch_real_subprocesses_with_isolated_fake_systemd(
    staged, switcher, monkeypatch, tmp_path
):
    """Exercise real Git validation, CLI subprocesses and systemctl invocation."""
    root, config, checkouts, state = staged
    fake_bin = tmp_path / "test-bin"
    fake_bin.mkdir()
    active = tmp_path / "simulated-active-python.txt"
    active.write_text(str(checkouts["v5.0.0a5"]))
    unit = tmp_path / "systemd" / "user" / "workgate-control.service"
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
        "unit = os.environ['WG_SIM_UNIT']\n"
        "print('LoadState=loaded')\n"
        "print('ActiveState=active')\n"
        "print('FragmentPath=' + unit)\n"
        "print(f'ExecStart={{ path={exe} ; argv[]={exe} -m workgate.main control --config {cfg} ; ignore_errors=no ; pid=1 ; }}')\n"
    )
    systemctl.chmod(0o700)

    for tag, executable in checkouts.items():
        executable.write_text(
            "#!/bin/sh\n"
            'if [ "$3" = "--version" ]; then\n'
            f'  echo "workgate {tag[1:]}"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$3" = "control" ] && [ "$4" = "install-service" ]; then\n'
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
    switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert active.read_text() == str(checkouts["v5.0.0a6"])
    assert switcher._old_python(root) == checkouts["v5.0.0a6"]

    switcher.switch(root=root, tag="v5.0.0a5", control_config=config)
    assert active.read_text() == str(checkouts["v5.0.0a5"])

    monkeypatch.setenv("WG_SIM_FAIL_NEW", str(checkouts["v5.0.0a6"]))
    with pytest.raises(RuntimeError, match="previous service restored"):
        switcher.switch(root=root, tag="v5.0.0a6", control_config=config)
    assert active.read_text() == str(checkouts["v5.0.0a5"])
    assert switcher._old_python(root) == checkouts["v5.0.0a5"]


def test_switch_native_start_mismatch_recovers_old_version(
    staged, switcher, monkeypatch
):
    root, config, checkouts, state = staged
    native = switcher._native

    def misreport_new():
        response = native()
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
