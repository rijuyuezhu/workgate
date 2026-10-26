"""Windows staged source task cutover, with no live Task Scheduler mutations."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def switcher(monkeypatch):
    source = Path(__file__).resolve().parents[2] / "deploy" / "source"
    monkeypatch.syspath_prepend(str(source))
    spec = importlib.util.spec_from_file_location(
        "wg_source_switch_windows", source / "switch_windows.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", "win32")
    for key in tuple(os.environ):
        if key.startswith("WORKGATE_"):
            monkeypatch.delenv(key)
    return module


@pytest.fixture
def native_switch(tmp_path, switcher, monkeypatch):
    root = tmp_path / "managed"
    config = tmp_path / "control config.yaml"
    config.write_text("mode: http\n")
    executor = tmp_path / "executor config.yaml"
    executor.write_text("state_dir: stable\n")

    old = root / "versions" / ("a" * 40) / ".venv/Scripts/python.exe"
    new = root / "versions" / ("b" * 40) / ".venv/Scripts/python.exe"
    for executable in (old, new):
        executable.parent.mkdir(parents=True)
        executable.touch()
        executable.with_name("pythonw.exe").touch()
    state = {
        "active": old,
        "calls": [],
        "stops": [],
        "error_new": False,
        "error_old": False,
        "fail_stop": False,
    }
    state["role"] = "control"
    state["config"] = config
    state["executor"] = executor
    state["root"] = root
    state["old"] = old
    state["new"] = new
    service_dir = tmp_path / "control" / "service"
    service_dir.mkdir(parents=True)
    launcher = service_dir / "launcher.py"
    state["directory"] = service_dir

    def content():
        if state["role"] == "control":
            return f"main(['control', '--config', {str(config)!r}])\n"
        return (
            "main("
            + repr(
                [
                    "colocate",
                    "--control-config",
                    str(config),
                    "--executor-config",
                    str(executor),
                ]
            )
            + ")\n"
        )

    launcher.write_text(content())
    state["content"] = content
    native = {
        "state": "Running",
        "action": {
            "execute": str(old.with_name("pythonw.exe")),
            "arguments": subprocess.list2cmdline([str(launcher)]),
            "working_directory": str(service_dir),
        },
    }
    state["native"] = native
    state["stable"] = old

    def native_query(python, role):
        assert role == state["role"]
        return native

    def install(python, role, control, extra):
        assert role == state["role"]
        assert control == config
        assert extra == (executor if role == "colocate" else None)
        state["calls"].append(python)
        native["action"]["execute"] = str(python.with_name("pythonw.exe"))
        native["state"] = "Running"
        launcher.write_text(content())
        state["active"] = python
        if python == new and state["error_new"]:
            raise RuntimeError("simulated failed new task")
        if python == old and state["error_old"]:
            raise RuntimeError("simulated failed recovery")

    def stable_writer(directory, python):
        assert directory == root
        state["stable"] = python

    def stop(python, role):
        assert role == state["role"]
        state["stops"].append(python)
        if not state["fail_stop"]:
            native["state"] = "Ready"

    state["native_query_impl"] = switcher._native
    state["stop_impl"] = switcher._stop
    state["install_impl"] = switcher._install
    monkeypatch.setattr(switcher, "_stop", stop)
    monkeypatch.setattr(switcher, "_old_python", lambda _: state["stable"])
    monkeypatch.setattr(
        switcher,
        "_staged_python",
        lambda _root, tag: new if tag == "v5.0.0a6" else old,
    )
    monkeypatch.setattr(switcher, "_native", native_query)
    monkeypatch.setattr(switcher, "_install", install)
    monkeypatch.setattr(switcher, "_write_launcher", stable_writer)
    return state


@pytest.mark.parametrize("role", ["control", "colocate"])
def test_windows_switch_noop_rollback_and_existing_configs(
    native_switch, switcher, role
):
    state = native_switch
    state["role"] = role
    if role == "colocate":
        relocated = state["directory"].parent.parent / "colocate" / "service"
        relocated.mkdir(parents=True)
        (relocated / "launcher.py").write_text(state["content"]())
        state["directory"] = relocated
        state["native"]["action"]["working_directory"] = str(relocated)
        state["native"]["action"]["arguments"] = subprocess.list2cmdline(
            [str(relocated / "launcher.py")]
        )
    kwargs = {
        "root": state["root"],
        "role": role,
        "control_config": state["config"],
        "service_dir": state["directory"],
        "executor_config": state["executor"] if role == "colocate" else None,
    }
    before = (state["config"].read_bytes(), state["executor"].read_bytes())
    switcher.switch(tag="v5.0.0a6", **kwargs)
    assert state["stable"] == state["new"]
    assert state["calls"] == [state["new"]]
    switcher.switch(tag="v5.0.0a6", **kwargs)
    assert state["calls"] == [state["new"]]
    switcher.switch(tag="v5.0.0a5", **kwargs)
    assert state["calls"][-1] == state["old"]
    assert state["stable"] == state["old"]
    assert before == (
        state["config"].read_bytes(),
        state["executor"].read_bytes(),
    )


def test_windows_failed_install_restores_old_task_and_stable_command(
    native_switch, switcher
):
    state = native_switch
    state["error_new"] = True
    with pytest.raises(RuntimeError, match="previous task restored"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            service_dir=state["directory"],
        )
    assert state["calls"] == [state["new"], state["old"]]
    assert state["stable"] == state["old"]
    assert state["active"] == state["old"]


def test_windows_failed_recovery_reports_both(native_switch, switcher):
    state = native_switch
    state["error_new"] = state["error_old"] = True
    with pytest.raises(RuntimeError, match="recovery also failed"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            service_dir=state["directory"],
        )
    assert state["stable"] == state["old"]


@pytest.mark.parametrize(
    "tamper",
    [
        "missing-task",
        "not-running",
        "wrong-interpreter",
        "wrong-launcher",
        "wrong-working-directory",
        "wrong-config",
        "multiple-actions",
    ],
)
def test_windows_refuses_unexpected_owned_task_action(
    native_switch, switcher, monkeypatch, tamper
):
    state = native_switch
    action = state["native"]["action"]
    if tamper == "missing-task":
        monkeypatch.setattr(switcher, "_native", lambda _python, _role: {})
    elif tamper == "not-running":
        state["native"]["state"] = "Ready"
    elif tamper == "wrong-interpreter":
        action["execute"] = "C:\\other\\pythonw.exe"
    elif tamper == "wrong-launcher":
        action["arguments"] = "other.py"
    elif tamper == "wrong-working-directory":
        action["working_directory"] = "C:\\other"
    elif tamper == "wrong-config":
        (state["directory"] / "launcher.py").write_text(
            "main(['control', '--config', 'other.yaml'])"
        )
    else:
        state["native"]["action"] = None
    with pytest.raises(RuntimeError, match="expected one|bound to"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            service_dir=state["directory"],
        )
    assert state["calls"] == []
    assert state["stable"] == state["old"]


def test_windows_staged_version_requires_pythonw(native_switch, switcher):
    state = native_switch
    state["new"].with_name("pythonw.exe").unlink()
    with pytest.raises(RuntimeError, match="pythonw.exe"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            service_dir=state["directory"],
        )
    assert state["calls"] == []


def test_windows_excludes_standalone_executor_and_wrong_config(
    native_switch, switcher
):
    state = native_switch
    with pytest.raises(
        ValueError, match="does not apply to standalone Executor"
    ):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="executor",
            control_config=state["config"],
            service_dir=state["directory"],
        )
    with pytest.raises(RuntimeError, match="durable YAML"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            executor_config=state["executor"],
            service_dir=state["directory"],
        )


def test_windows_only(native_switch, switcher, monkeypatch):
    state = native_switch
    monkeypatch.setattr(switcher.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows Task Scheduler"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            service_dir=state["directory"],
        )


def test_windows_stops_old_task_before_install_and_stops_new_on_recovery(
    native_switch, switcher, monkeypatch
):
    state = native_switch
    state["error_new"] = True
    seen = []

    def stop(python, role):
        seen.append(("stop", python))
        state["native"]["state"] = "Ready"

    old_install = switcher._install

    def install(python, role, control, executor):
        seen.append(("install", python))
        old_install(python, role, control, executor)

    monkeypatch.setattr(switcher, "_stop", stop)
    monkeypatch.setattr(switcher, "_install", install)
    with pytest.raises(RuntimeError, match="previous task restored"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            service_dir=state["directory"],
        )
    assert seen == [
        ("stop", state["old"]),
        ("install", state["new"]),
        ("stop", state["old"]),
        ("install", state["old"]),
    ]


def test_windows_refuses_to_install_if_task_wont_stop(native_switch, switcher):
    state = native_switch
    state["fail_stop"] = True
    with pytest.raises(RuntimeError, match="recovery also failed"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            service_dir=state["directory"],
        )
    assert state["calls"] == []
    assert state["stable"] == state["old"]


@pytest.mark.parametrize(
    "case", ["not-running", "wrong-python", "wrong-config"]
)
def test_windows_new_native_task_must_activate_or_rollback(
    native_switch, switcher, monkeypatch, case
):
    state = native_switch
    original_install = switcher._install

    def install(python, role, config, executor):
        original_install(python, role, config, executor)
        if python == state["new"]:
            if case == "not-running":
                state["native"]["state"] = "Ready"
            elif case == "wrong-python":
                state["native"]["action"]["execute"] = (
                    "C:\\unexpected\\pythonw.exe"
                )
            else:
                (state["directory"] / "launcher.py").write_text(
                    "main(['control', '--config', 'wrong.yaml'])"
                )

    monkeypatch.setattr(switcher, "_install", install)
    with pytest.raises(RuntimeError, match="previous task restored"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            service_dir=state["directory"],
        )
    assert state["calls"] == [state["new"], state["old"]]
    assert state["stable"] == state["old"]


def test_windows_queries_existing_owned_task_through_runtime_helper(
    native_switch, switcher, monkeypatch
):
    state = native_switch
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            '{"state":"Running","action":{"execute":"pythonw.exe","arguments":"launcher.py","working_directory":"C:\\\\tmp"}}',
            "",
        )

    monkeypatch.setattr(switcher.subprocess, "run", run)
    native = state["native_query_impl"](state["old"], "control")
    assert native["state"] == "Running"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == str(state["old"])
    assert argv[1] == "-c"
    assert "WindowsUserTask" in argv[2]
    assert "Workgate Control" in argv[2]
    assert "Managed by Workgate: control" in argv[2]
    assert kwargs["check"] is True


def test_windows_uses_existing_cli_for_stop_and_install(
    native_switch, switcher, monkeypatch
):
    state = native_switch
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(switcher.subprocess, "run", run)
    state["stop_impl"](state["old"], "control")
    state["install_impl"](state["new"], "control", state["config"], None)
    assert calls[0][0][1:] == ["-m", "workgate.main", "control", "stop-service"]
    assert calls[1][0][1:5] == [
        "-m",
        "workgate.main",
        "control",
        "install-service",
    ]
    assert calls[1][0][-2:] == ["--config", str(state["config"])]


def test_windows_paths_case_insensitive(native_switch, switcher):
    assert switcher._same_path(
        r"C:\Users\Somebody\Apps\PYTHONW.EXE",
        r"c:/users/somebody/apps/pythonw.exe",
    )


@pytest.mark.skipif(
    os.name != "nt", reason="requires native Windows .cmd path rules"
)
def test_native_windows_parses_prepared_managed_cmd(tmp_path, switcher):
    root = tmp_path / "root with spaces"
    python = root / "versions" / ("a" * 40) / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    switcher._write_launcher(root, python)
    assert switcher._old_python(root) == python
    (root / "bin/workgate.cmd").write_text(
        "@echo off\r\nrem unrelated\r\nexit\r\n"
    )
    with pytest.raises(RuntimeError, match="unmanaged launcher"):
        switcher._old_python(root)


@pytest.fixture
def executor_switch(native_switch, switcher, monkeypatch, tmp_path):
    state = native_switch
    state["role"] = "executor"
    directory = tmp_path / "state" / "executor" / "service"
    directory.mkdir(parents=True)
    profile = directory.parent / "profile.json"
    profile.write_bytes(b'{"executor_id":"unchanged","token":"secret"}')
    (directory / "launcher.py").write_text("managed Executor runtime")
    state["directory"] = directory
    state["profile"] = profile
    state["native"]["action"]["working_directory"] = str(directory)
    state["native"]["action"]["arguments"] = subprocess.list2cmdline(
        [str(directory / "launcher.py")]
    )
    state["identity_before"] = profile.read_bytes()
    state["executor_install_calls"] = []
    state["stop_calls"] = []
    state["modify_identity"] = False
    state["bad_state"] = False

    def status(python, config):
        assert config == state["executor"]
        return {
            "State": "running",
            "Backend": "scheduled-task",
            "Log": str(directory / "executor.log"),
            "Runtime owner": "source",
            "Runtime": "current" if python == state["active"] else "stale",
        }

    def install(python, config):
        assert config == state["executor"]
        state["executor_install_calls"].append(python)
        state["native"]["state"] = "Running"
        state["native"]["action"]["execute"] = str(
            python.with_name("pythonw.exe")
        )
        state["active"] = python
        if state["modify_identity"] and python == state["new"]:
            profile.write_bytes(b"changed identity")
        if state["bad_state"] and python == state["new"]:
            state["native"]["state"] = "Ready"
        if state["error_new"] and python == state["new"]:
            raise RuntimeError("new Executor failed")
        if state["error_old"] and python == state["old"]:
            raise RuntimeError("old Executor failed")

    def stop(python, role):
        assert role == "executor"
        state["stop_calls"].append(python)
        state["native"]["state"] = "Ready"

    state["executor_status_impl"] = switcher._executor_status
    state["executor_install_impl"] = switcher._executor_install
    monkeypatch.setattr(switcher, "_executor_status", status)
    monkeypatch.setattr(switcher, "_executor_install", install)
    monkeypatch.setattr(switcher, "_stop", stop)
    return state


def test_windows_executor_switch_noop_rollback_preserves_identity(
    executor_switch, switcher
):
    state = executor_switch
    args = dict(
        root=state["root"], role="executor", executor_config=state["executor"]
    )
    switcher.switch(tag="v5.0.0a6", **args)
    assert state["executor_install_calls"] == [state["new"]]
    assert state["stable"] == state["new"]
    assert state["profile"].read_bytes() == state["identity_before"]
    switcher.switch(tag="v5.0.0a6", **args)
    assert state["executor_install_calls"] == [state["new"]]
    switcher.switch(tag="v5.0.0a5", **args)
    assert state["executor_install_calls"] == [state["new"], state["old"]]
    assert state["stable"] == state["old"]
    assert state["profile"].read_bytes() == state["identity_before"]


@pytest.mark.parametrize(
    "failure", ["install", "native-not-running", "identity-changed"]
)
def test_windows_executor_failure_restores_old_task(
    executor_switch, switcher, failure
):
    state = executor_switch
    if failure == "install":
        state["error_new"] = True
    elif failure == "native-not-running":
        state["bad_state"] = True
    else:
        state["modify_identity"] = True
    with pytest.raises(
        RuntimeError, match="previous task restored|recovery also failed"
    ):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="executor",
            executor_config=state["executor"],
        )
    assert state["executor_install_calls"] == [state["new"], state["old"]]
    assert state["stable"] == state["old"]
    if failure != "identity-changed":
        assert state["profile"].read_bytes() == state["identity_before"]


@pytest.mark.parametrize(
    "mismatch", ["missing-profile", "foreign-action", "stale", "other-log"]
)
def test_windows_executor_refuses_unmanaged_state(
    executor_switch, switcher, monkeypatch, mismatch
):
    state = executor_switch
    if mismatch == "missing-profile":
        state["profile"].unlink()
    elif mismatch == "foreign-action":
        state["native"]["action"]["execute"] = "X:/foreign/pythonw.exe"
    elif mismatch == "stale":
        orig = switcher._executor_status

        def stale_status(python, config):
            result = orig(python, config)
            result["Runtime"] = "stale"
            return result

        monkeypatch.setattr(switcher, "_executor_status", stale_status)
    else:
        orig = switcher._executor_status

        def moved_status(python, config):
            result = orig(python, config)
            if python == state["new"]:
                result["Log"] = str(state["directory"] / "moved.log")
            return result

        monkeypatch.setattr(switcher, "_executor_status", moved_status)
    with pytest.raises(RuntimeError):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="executor",
            executor_config=state["executor"],
        )
    assert state["executor_install_calls"] == []
    assert state["stable"] == state["old"]


def test_windows_executor_failed_rollback_reports_failure(
    executor_switch, switcher
):
    state = executor_switch
    state["error_new"] = state["error_old"] = True
    with pytest.raises(RuntimeError, match="recovery also failed"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="executor",
            executor_config=state["executor"],
        )
    assert state["stable"] == state["old"]


def test_windows_executor_does_not_allow_control_yaml(
    executor_switch, switcher
):
    state = executor_switch
    with pytest.raises(ValueError, match="Control config"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="executor",
            executor_config=state["executor"],
            control_config=state["config"],
        )
    assert state["executor_install_calls"] == []


def test_executor_actual_cli_lifecycle_names():
    """Catch mismatches that fake source CLIs cannot detect."""
    import sys

    for action in ("status", "stop", "install-service"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "workgate.main",
                "executor",
                action,
                "--help",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert f"executor {action}" in result.stdout


def test_executor_native_stop_uses_actual_cli(
    executor_switch, switcher, monkeypatch
):
    state = executor_switch
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(switcher.subprocess, "run", run)
    state["stop_impl"](state["old"], "executor")
    assert commands == [
        [str(state["old"]), "-m", "workgate.main", "executor", "stop"]
    ]


def test_executor_status_and_install_use_actual_cli_verbs(
    executor_switch, switcher, monkeypatch
):
    state = executor_switch
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        stdout = (
            "State: running\nBackend: scheduled-task\n"
            "Runtime owner: source\nRuntime: current\n"
            f"Log: {state['directory'] / 'executor.log'}\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(switcher.subprocess, "run", run)
    status = state["executor_status_impl"](state["old"], state["executor"])
    state["executor_install_impl"](state["new"], state["executor"])
    assert status["State"] == "running"
    assert calls == [
        [
            str(state["old"]),
            "-m",
            "workgate.main",
            "executor",
            "status",
            "--config",
            str(state["executor"]),
        ],
        [
            str(state["new"]),
            "-m",
            "workgate.main",
            "executor",
            "install-service",
            "--config",
            str(state["executor"]),
        ],
    ]


@pytest.fixture
def legacy_executor_switch(executor_switch, switcher, tmp_path, monkeypatch):
    """Legacy unversioned source task, without a managed stable CLI."""
    state = executor_switch
    old = tmp_path / "unversioned" / ".venv-native" / "Scripts" / "python.exe"
    old.parent.mkdir(parents=True)
    old.touch()
    old.with_name("pythonw.exe").touch()
    state["old"] = old
    state["active"] = old
    state["stable"] = None
    state["native"]["action"]["execute"] = str(old.with_name("pythonw.exe"))
    monkeypatch.setattr(
        switcher,
        "_old_python",
        lambda _: pytest.fail(
            "initial adoption must not require a versioned CLI"
        ),
    )
    return state


def test_windows_legacy_executor_initial_adoption_retains_pairing(
    legacy_executor_switch, switcher
):
    state = legacy_executor_switch
    assert not (state["root"] / "bin/workgate.cmd").exists()
    original_config = state["executor"].read_bytes()
    switcher.switch(
        root=state["root"],
        tag="v5.0.0a6",
        role="executor",
        executor_config=state["executor"],
        adopt_legacy_executor=True,
    )
    assert state["executor_install_calls"] == [state["new"]]
    assert state["stable"] == state["new"]
    assert state["active"] == state["new"]
    assert state["profile"].read_bytes() == state["identity_before"]
    assert state["executor"].read_bytes() == original_config


@pytest.mark.parametrize(
    "failure", ["install", "native-not-running", "identity-changed"]
)
def test_windows_legacy_executor_adoption_restores_old_task(
    legacy_executor_switch, switcher, failure
):
    state = legacy_executor_switch
    if failure == "install":
        state["error_new"] = True
    elif failure == "native-not-running":
        state["bad_state"] = True
    else:
        state["modify_identity"] = True

    with pytest.raises(
        RuntimeError, match="previous task restored|recovery also failed"
    ):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="executor",
            executor_config=state["executor"],
            adopt_legacy_executor=True,
        )
    assert state["executor_install_calls"] == [state["new"], state["old"]]
    assert state["active"] == state["old"]
    assert state["stable"] is None
    assert not (state["root"] / "bin/workgate.cmd").exists()
    if failure != "identity-changed":
        assert state["profile"].read_bytes() == state["identity_before"]


@pytest.mark.parametrize(
    "invalid",
    [
        "existing-managed-launcher",
        "unmanaged-launcher",
        "not-running",
        "foreign-executable",
        "missing-python",
        "missing-profile",
        "wrong-config",
    ],
)
def test_windows_legacy_executor_adoption_rejects_unsafe_preconditions(
    legacy_executor_switch, switcher, monkeypatch, invalid
):
    state = legacy_executor_switch
    if invalid == "existing-managed-launcher":
        launcher = state["root"] / "bin/workgate.cmd"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(
            "@echo off\r\nrem Managed by Workgate source preparation\r\n"
            "echo managed\r\n"
        )
    elif invalid == "unmanaged-launcher":
        launcher = state["root"] / "bin/workgate.cmd"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("@echo off\r\necho foreign\r\n")
    elif invalid == "not-running":
        state["native"]["state"] = "Ready"
    elif invalid == "foreign-executable":
        state["native"]["action"]["execute"] = str(
            state["old"].with_name("foreign.exe")
        )
    elif invalid == "missing-python":
        state["old"].unlink()
    elif invalid == "missing-profile":
        state["profile"].unlink()
    elif invalid == "wrong-config":
        original = switcher._executor_status

        def wrong_status(python, config):
            data = original(python, config)
            if python == state["new"]:
                data["Log"] = str(state["directory"] / "moved.log")
            return data

        monkeypatch.setattr(switcher, "_executor_status", wrong_status)

    with pytest.raises(RuntimeError):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="executor",
            executor_config=state["executor"],
            adopt_legacy_executor=True,
        )
    assert state["executor_install_calls"] == []
    assert state["active"] == state["old"]


def test_windows_legacy_adoption_rejects_other_role(native_switch, switcher):
    state = native_switch
    with pytest.raises(ValueError, match="only for standalone Executor"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="control",
            control_config=state["config"],
            adopt_legacy_executor=True,
        )
    assert state["calls"] == []


def test_windows_legacy_executor_failed_stable_command_restores_old(
    legacy_executor_switch, switcher, monkeypatch
):
    state = legacy_executor_switch
    launcher = state["root"] / "bin/workgate.cmd"

    def fail_after_writing(root, python):
        assert python == state["new"]
        launcher.parent.mkdir(parents=True)
        launcher.write_text("incomplete new stable command")
        raise OSError("cannot install stable command")

    monkeypatch.setattr(switcher, "_write_launcher", fail_after_writing)
    with pytest.raises(RuntimeError, match="previous task restored"):
        switcher.switch(
            root=state["root"],
            tag="v5.0.0a6",
            role="executor",
            executor_config=state["executor"],
            adopt_legacy_executor=True,
        )
    assert state["executor_install_calls"] == [state["new"], state["old"]]
    assert state["active"] == state["old"]
    assert state["profile"].read_bytes() == state["identity_before"]
    assert not launcher.exists()
