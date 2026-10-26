"""Role-specific source orchestration; always reuse the pinned preparer."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clean_workgate_env(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("WORKGATE_"):
            monkeypatch.delenv(name)


@pytest.fixture
def installer(monkeypatch):
    location = Path(__file__).resolve().parents[2] / "deploy" / "source"
    monkeypatch.syspath_prepend(str(location))
    spec = importlib.util.spec_from_file_location(
        "wg_install", location / "install.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def skip_native_status(installer, monkeypatch):
    monkeypatch.setattr(
        installer, "_check_native_services", lambda *a, **kw: None
    )


@pytest.mark.parametrize(
    "role,expected",
    [
        (
            "control",
            [
                ["control", "init-config"],
                ["control", "install-service"],
            ],
        ),
        (
            "executor",
            [
                ["executor", "setup", "https://control.example"],
            ],
        ),
        (
            "colocate",
            [
                ["colocate", "setup"],
            ],
        ),
    ],
)
@pytest.mark.usefixtures("skip_native_status")
def test_install_command_order_and_only_one_native_role(
    installer, tmp_path, monkeypatch, role, expected
):
    checkouts = []
    calls = []

    def prepare(**kw):
        checkouts.append(kw)
        return tmp_path / "versions" / "full-sha"

    monkeypatch.setattr(installer, "prepare", prepare)

    def invoke(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(installer.subprocess, "run", invoke)
    checkout = installer.install(
        role=role,
        tag="v5.0.0a6",
        root=tmp_path,
        control_url="https://control.example" if role == "executor" else None,
    )
    assert checkout == tmp_path / "versions" / "full-sha"
    assert checkouts == [
        {
            "tag": "v5.0.0a6",
            "role": role,
            "root": tmp_path,
            "activate_launcher": False,
            "expected_commit": None,
            "arm64_wheel": None,
            "arm64_wheel_sha256": None,
        }
    ]
    assert [args[3:] for args, _kw in calls] == expected
    for args, kw in calls:
        assert args[:3] == [
            str(installer._python(checkout)),
            "-m",
            "workgate.main",
        ]
        assert kw["check"] is True
        assert kw["cwd"] == checkout


@pytest.mark.parametrize(
    "options,expected",
    [
        (
            {
                "role": "control",
                "control_config": Path("ctl.yaml"),
                "host": "0.0.0.0",
                "port": 7777,
                "base_url": "https://workgate.example",
            },
            [
                [
                    "control",
                    "init-config",
                    "--config",
                    "ctl.yaml",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "7777",
                    "--base-url",
                    "https://workgate.example",
                ],
                ["control", "install-service", "--config", "ctl.yaml"],
            ],
        ),
        (
            {
                "role": "executor",
                "control_url": "https://workgate.example",
                "executor_config": Path("executor.yaml"),
                "name": "host",
            },
            [
                [
                    "executor",
                    "setup",
                    "https://workgate.example",
                    "--name",
                    "host",
                    "--config",
                    "executor.yaml",
                ]
            ],
        ),
        (
            {
                "role": "colocate",
                "name": "host",
                "control_config": Path("ctl.yaml"),
                "executor_config": Path("executor.yaml"),
            },
            [
                [
                    "colocate",
                    "setup",
                    "--name",
                    "host",
                    "--control-config",
                    "ctl.yaml",
                    "--executor-config",
                    "executor.yaml",
                ]
            ],
        ),
    ],
)
def test_role_flags_are_passed_only_to_matching_existing_cli(
    installer, options, expected
):
    defaults = dict(
        role="control",
        control_url=None,
        name=None,
        control_config=None,
        executor_config=None,
        host=None,
        port=None,
        base_url=None,
    )
    assert installer.role_commands(**(defaults | options)) == expected


@pytest.mark.parametrize(
    "invalid",
    [
        {"role": "executor"},
        {"role": "executor", "control_url": "http://evil.example"},
        {"role": "executor", "control_url": "https://user:pass@host"},
        {"role": "executor", "control_url": "https://host?token=abc"},
        {"role": "executor", "control_url": "https://host#fragment"},
        {"role": "executor", "control_url": "https://host:abc"},
        {"role": "colocate", "control_url": "https://host"},
        {"role": "control", "name": "host"},
        {"role": "control", "executor_config": Path("executor.yaml")},
        {"role": "control", "port": 65536},
        {
            "role": "executor",
            "control_url": "https://host",
            "host": "localhost",
        },
        {"role": "colocate", "base_url": "https://host"},
        {"role": "broken"},
    ],
)
def test_invalid_role_options_fail_before_git_or_service_change(
    installer, tmp_path, monkeypatch, invalid
):
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail("must validate flags before source checkout"),
    )
    defaults = dict(
        role="control",
        tag="v5.0.0a6",
        root=tmp_path,
        control_url=None,
        name=None,
        control_config=None,
        executor_config=None,
        host=None,
        port=None,
        base_url=None,
    )
    with pytest.raises(ValueError):
        installer.install(**(defaults | invalid))


def test_environment_overrides_fail_before_version_prepare(
    installer, tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_HOST", "0.0.0.0")
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail(
            "must not switch runtime with ambient overrides"
        ),
    )
    with pytest.raises(RuntimeError, match="durable YAML"):
        installer.install(role="control", tag="v5.0.0a6", root=tmp_path)


def test_public_control_bind_without_oauth_base_url_fails_before_prepare(
    installer, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail(
            "must not switch runtime for bad public OAuth config"
        ),
    )
    with pytest.raises(ValueError, match="base-url"):
        installer.install(
            role="control",
            host="0.0.0.0",
            tag="v5.0.0a6",
            root=tmp_path,
        )


@pytest.mark.usefixtures("skip_native_status")
def test_failed_pairing_never_runs_service_or_later_commands(
    installer, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        installer, "prepare", lambda **kw: tmp_path / "versions" / "sha"
    )
    calls = []

    def reject(args, **kw):
        calls.append(args[3:])
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(installer.subprocess, "run", reject)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(role="control", tag="v5.0.0a6", root=tmp_path)
    assert calls == [["control", "init-config"]]


@pytest.mark.usefixtures("skip_native_status")
def test_no_uv_run_reentry_and_no_shell_in_role_service(
    installer, tmp_path, monkeypatch
):
    monkeypatch.setattr(installer, "prepare", lambda **kw: tmp_path / "version")
    launches = []
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda cmd, **kw: launches.append((cmd, kw)),
    )
    installer.install(role="colocate", tag="v5.0.0a6", root=tmp_path)
    assert len(launches) == 1
    cmd, kw = launches[0]
    assert cmd[:3] == [
        str(installer._python(tmp_path / "version")),
        "-m",
        "workgate.main",
    ]
    assert kw.get("shell") is not True
    assert "uv" not in cmd


@pytest.mark.usefixtures("skip_native_status")
def test_failed_first_role_setup_keeps_previous_stable_launcher(
    installer, tmp_path, monkeypatch
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stable = bin_dir / ("workgate.cmd" if os.name == "nt" else "workgate")
    stable.write_text(
        ("@echo off\nrem " if os.name == "nt" else "#!/bin/sh\n# ")
        + "Managed by Workgate source preparation\n"
        + "previous managed release\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: tmp_path / "versions" / "new-sha",
    )

    def reject(args, **kw):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(installer.subprocess, "run", reject)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(
            role="executor",
            tag="v5.0.0a6",
            root=tmp_path,
            control_url="https://control.example",
        )
    assert stable.read_text().endswith("previous managed release\n")


def test_unmanaged_launcher_blocks_service_setup_before_any_changes(
    installer, tmp_path, monkeypatch
):
    directory = tmp_path / "bin"
    directory.mkdir()
    launcher = directory / ("workgate.cmd" if os.name == "nt" else "workgate")
    launcher.write_text("unrelated owner")
    monkeypatch.setattr(
        installer, "prepare", lambda **kw: tmp_path / "versions" / "new-sha"
    )
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kw: pytest.fail("must not configure services"),
    )
    with pytest.raises(RuntimeError, match="unmanaged launcher"):
        installer.install(role="control", tag="v5.0.0a6", root=tmp_path)
    assert launcher.read_text() == "unrelated owner"


@pytest.mark.usefixtures("skip_native_status")
def test_successful_role_setup_activates_stable_launcher_once(
    installer, tmp_path, monkeypatch
):
    checkout = tmp_path / "versions" / "new-sha"
    monkeypatch.setattr(installer, "prepare", lambda **kw: checkout)
    events = []
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda args, **kw: events.append("setup"),
    )
    monkeypatch.setattr(
        installer,
        "_write_launcher",
        lambda root, python: events.append(("switch", root, python)),
    )
    installer.install(role="control", tag="v5.0.0a6", root=tmp_path)
    assert events == [
        "setup",
        "setup",
        ("switch", tmp_path, installer._python(checkout)),
    ]


@pytest.mark.usefixtures("skip_native_status")
def test_public_bootstrap_pinned_commit_reaches_source_preparer(
    installer, tmp_path, monkeypatch
):
    sha = "a" * 40
    received = []
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: (received.append(kw), tmp_path / "versions" / sha)[1],
    )
    monkeypatch.setattr(installer, "_check_launcher", lambda root: None)
    monkeypatch.setattr(
        installer,
        "_write_launcher",
        lambda root, python: root / "bin" / "workgate",
    )
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: None)
    installer.install(
        role="control",
        tag="v5.0.0a6",
        root=tmp_path,
        expected_commit=sha,
    )
    assert received[0]["expected_commit"] == sha
    assert received[0]["activate_launcher"] is False


@pytest.mark.parametrize(
    "role,expected",
    [
        ("control", ["control", "colocate"]),
        ("executor", ["executor", "colocate"]),
        ("colocate", ["control", "executor", "colocate"]),
    ],
)
def test_fresh_install_checks_conflicting_services_before_setup(
    installer, tmp_path, monkeypatch, role, expected
):
    checkout = tmp_path / "sha"
    monkeypatch.setattr(installer, "prepare", lambda **kw: checkout)
    monkeypatch.setattr(installer, "_write_launcher", lambda *args: tmp_path)
    observed = []

    def run(argv, **kwargs):
        candidate = argv[3]
        if kwargs.get("capture_output"):
            observed.append(candidate)
            value = (
                "State: not-installed\n"
                if candidate == "executor"
                else f"{candidate.capitalize()} service: not-installed\n"
            )
            return subprocess.CompletedProcess(argv, 0, value)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.install(
        role=role,
        root=tmp_path,
        tag="v5.0.0a6",
        control_url="https://control.example" if role == "executor" else None,
    )
    assert observed == expected


@pytest.mark.parametrize("role", ["control", "executor", "colocate"])
def test_installed_service_rejected_before_native_setup_or_launcher_change(
    installer, tmp_path, monkeypatch, role
):
    monkeypatch.setattr(installer, "prepare", lambda **kw: tmp_path / "sha")
    monkeypatch.setattr(
        installer,
        "_write_launcher",
        lambda *args: pytest.fail("must not switch existing launcher"),
    )
    called = []

    def run(argv, **kwargs):
        assert kwargs.get("capture_output"), "native install must not start"
        called.append(argv[3])
        prefix = (
            "State"
            if argv[3] == "executor"
            else f"{argv[3].capitalize()} service"
        )
        return subprocess.CompletedProcess(argv, 0, f"{prefix}: inactive\n")

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="already installed"):
        installer.install(
            role=role,
            root=tmp_path,
            tag="v5.0.0a6",
            control_url="https://control.example"
            if role == "executor"
            else None,
        )
    assert len(called) == 1


@pytest.mark.parametrize("output", ["", "unrecognized", "State: unknown\n"])
def test_unexpected_executor_native_status_fails_closed(
    installer, tmp_path, monkeypatch, output
):
    monkeypatch.setattr(installer, "prepare", lambda **kw: tmp_path / "sha")
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output),
    )
    with pytest.raises(RuntimeError, match="status response|already installed"):
        installer.install(
            role="executor",
            root=tmp_path,
            tag="v5.0.0a6",
            control_url="https://control.example",
        )


def test_custom_executor_config_flows_to_status_probe(
    installer, tmp_path, monkeypatch
):
    inspected = []

    def run(argv, **kwargs):
        inspected.append(argv[3:])
        response = (
            "State: not-installed\n"
            if argv[3] == "executor"
            else "Colocate service: not-installed\n"
        )
        return subprocess.CompletedProcess(argv, 0, response)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer._check_native_services(
        tmp_path / "python", tmp_path, "executor", tmp_path / "custom.yaml"
    )
    assert inspected[0] == [
        "executor",
        "status",
        "--config",
        str(tmp_path / "custom.yaml"),
    ]


@pytest.mark.parametrize("role", ["control", "colocate"])
def test_native_service_status_with_extra_lines_fails_closed(
    installer, tmp_path, monkeypatch, role
):
    prefix = role.capitalize()
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv,
            0,
            f"{prefix} service: not-installed\nwarning: unknown service owner\n",
        ),
    )
    with pytest.raises(RuntimeError, match="unexpected .* status response"):
        installer._check_native_services(
            tmp_path / "python", tmp_path, role, None
        )


def test_installer_forwards_explicit_arm64_wheel_pair(
    installer, tmp_path, monkeypatch
):
    passed = []

    def fake_prepare(**kwargs):
        passed.append(kwargs)
        raise RuntimeError("preparation stopped before role setup")

    monkeypatch.setattr(installer, "prepare", fake_prepare)
    with pytest.raises(RuntimeError, match="preparation stopped"):
        installer.install(
            role="control",
            tag="v5.0.0a6",
            root=tmp_path,
            arm64_wheel="https://example.com/cryptography-50.0.2-cp314-abi3-win_arm64.whl",
            arm64_wheel_sha256="a" * 64,
        )
    assert passed[0]["arm64_wheel_sha256"] == "a" * 64
    assert passed[0]["arm64_wheel"].startswith("https://")


@pytest.mark.parametrize(
    "role,conflicting",
    [
        ("control", "control"),
        ("executor", "executor"),
        ("colocate", "colocate"),
        ("executor", "colocate"),
        ("control", "colocate"),
    ],
)
def test_existing_service_fails_before_prepare(
    installer, monkeypatch, tmp_path, role, conflicting
):
    # A validated managed launcher is sufficient to reject a conflicting
    # service before clone/uv sync can touch the installed source runtime.
    existing_python = tmp_path / "versions" / "old" / "bin" / "python"
    installer._write_launcher(tmp_path, existing_python)
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail(
            "must not clone or sync while service installed"
        ),
    )
    observed = []

    def run(argv, **kw):
        assert kw.get("capture_output") is True
        assert argv[0] == str(
            tmp_path
            / "bin"
            / ("workgate.cmd" if os.name == "nt" else "workgate")
        )
        assert argv[1] in {"control", "executor", "colocate"}
        assert "-m" not in argv
        observed.append(argv[1])
        prefix = (
            "State: "
            if argv[1] == "executor"
            else f"{argv[1].capitalize()} service: "
        )
        state = "inactive" if argv[1] == conflicting else "not-installed"
        return subprocess.CompletedProcess(argv, 0, prefix + state + "\n")

    monkeypatch.setattr(installer.subprocess, "run", run)
    before = (
        tmp_path / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    ).read_bytes()
    with pytest.raises(RuntimeError, match="already installed"):
        installer.install(
            role=role,
            root=tmp_path,
            tag="v5.0.0a6",
            control_url="https://control.example"
            if role == "executor"
            else None,
        )
    assert conflicting in observed
    assert (
        tmp_path / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    ).read_bytes() == before
    assert not (tmp_path / "versions" / "old").exists()


def test_existing_managed_launcher_allows_fresh_install_if_no_service(
    installer, monkeypatch, tmp_path
):
    installer._write_launcher(
        tmp_path, tmp_path / "versions" / "old" / "python"
    )
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if kw.get("capture_output"):
            name = (
                argv[1]
                if argv[1] in ("control", "executor", "colocate")
                else argv[3]
            )
            prefix = (
                "State: "
                if name == "executor"
                else f"{name.capitalize()} service: "
            )
            return subprocess.CompletedProcess(
                argv, 0, prefix + "not-installed\n"
            )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(
        installer, "prepare", lambda **kw: tmp_path / "versions" / "new"
    )
    installer.install(role="control", tag="v5.0.0a6", root=tmp_path)
    status_calls = [argv for argv, kw in calls if kw.get("capture_output")]
    assert len(status_calls) == 4  # before and after preparation
    assert status_calls[0][1] == "control"
    assert status_calls[2][1:4] == ["-m", "workgate.main", "control"]


def test_old_managed_launcher_without_status_falls_back_to_prepared_cli(
    installer, monkeypatch, tmp_path
):
    installer._write_launcher(
        tmp_path, tmp_path / "versions" / "old" / "python"
    )
    prepared = []

    def prepare(**kwargs):
        prepared.append(True)
        return tmp_path / "versions" / "new"

    def run(argv, **kwargs):
        if argv[1] == "control":
            raise subprocess.CalledProcessError(
                2, argv
            )  # old CLI lacks status-service
        if kwargs.get("capture_output"):
            role = argv[3]
            return subprocess.CompletedProcess(
                argv, 0, f"{role.capitalize()} service: not-installed\n"
            )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(installer, "prepare", prepare)
    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.install(role="control", tag="v5.0.0a6", root=tmp_path)
    assert prepared == [True]


def test_unmanaged_launcher_refused_before_clone(
    installer, monkeypatch, tmp_path
):
    launcher = (
        tmp_path / "bin" / ("workgate.cmd" if os.name == "nt" else "workgate")
    )
    launcher.parent.mkdir(parents=True)
    launcher.write_text("not Workgate managed")
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail(
            "unmanaged launcher must reject before prepare"
        ),
    )
    with pytest.raises(RuntimeError, match="unmanaged launcher"):
        installer.install(role="control", tag="v5.0.0a6", root=tmp_path)


def test_existing_launcher_unknown_status_fails_before_clone(
    installer, monkeypatch, tmp_path
):
    installer._write_launcher(
        tmp_path, tmp_path / "versions" / "old" / "python"
    )
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail(
            "status uncertainty must reject before prepare"
        ),
    )
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "unknown\n"),
    )
    with pytest.raises(RuntimeError, match="unexpected control status"):
        installer.install(role="control", tag="v5.0.0a6", root=tmp_path)


@pytest.mark.usefixtures("skip_native_status")
@pytest.mark.parametrize("role", ["control", "executor", "colocate"])
def test_relative_config_paths_are_resolved_before_versioned_role_setup(
    installer, tmp_path, monkeypatch, role
):
    # Public/bootstrap installers launch the role CLI from the release checkout.
    # Relative YAML paths must remain relative to the invoking user's directory.
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    control_file = config_dir / "control.yaml"
    executor_file = config_dir / "executor.yaml"
    control_file.write_text("mode: http\n")
    executor_file.write_text("max_jobs: 8\n")
    checkout = tmp_path / "managed" / "versions" / "sha"
    monkeypatch.setattr(installer, "prepare", lambda **kwargs: checkout)
    calls = []

    def invoke(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(installer.subprocess, "run", invoke)
    installer.install(
        role=role,
        tag="v5.0.0a6",
        root=tmp_path / "managed",
        control_url="https://control.example" if role == "executor" else None,
        control_config=Path("config/control.yaml")
        if role != "executor"
        else None,
        executor_config=Path("config/executor.yaml")
        if role != "control"
        else None,
    )
    assert calls
    for argv, kwargs in calls:
        assert kwargs["cwd"] == checkout
        assert "config/control.yaml" not in argv
        assert "config/executor.yaml" not in argv
        if "--control-config" in argv:
            assert argv[argv.index("--control-config") + 1] == str(control_file)
        if "--executor-config" in argv:
            assert argv[argv.index("--executor-config") + 1] == str(
                executor_file
            )
        if "--config" in argv:
            expected = executor_file if role == "executor" else control_file
            assert argv[argv.index("--config") + 1] == str(expected)


@pytest.mark.usefixtures("skip_native_status")
def test_tilde_control_config_uses_caller_home_not_checkout(
    installer, tmp_path, monkeypatch
):
    checkout = tmp_path / "versions" / "sha"
    monkeypatch.setattr(installer, "prepare", lambda **kwargs: checkout)
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(installer.subprocess, "run", invoke)
    installer.install(
        role="control",
        tag="v5.0.0a6",
        root=tmp_path,
        control_config=Path("~/wg-test-control.yaml"),
    )
    expected = str(Path.home() / "wg-test-control.yaml")
    assert any(expected in argv for argv in calls)


@pytest.mark.parametrize("role", ["control", "executor", "colocate"])
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_installer_exact_same_release_is_noop_without_uv_or_restart(
    installer, monkeypatch, tmp_path, role, platform
):
    switch = importlib.import_module("switch")
    switch_windows = importlib.import_module("switch_windows")

    monkeypatch.setattr(installer.sys, "platform", platform)
    root = tmp_path / "source"
    root.mkdir()
    commit = "b" * 40
    checkout = root / "versions" / commit
    python = (
        checkout
        / ".venv"
        / ("Scripts/python.exe" if platform == "win32" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(
        switch_windows if platform == "win32" else switch,
        "_old_python",
        lambda _root: python,
    )
    monkeypatch.setattr(switch, "_staged_python", lambda _root, _tag: python)
    config = root / "executor.yaml"
    config.write_text("state_dir: stable")
    opts = dict(
        control_url="https://control.example" if role == "executor" else None,
        name="existing" if role != "control" else None,
        control_config=None,
        executor_config=config if role == "executor" else None,
        host=None,
        port=None,
        base_url=None,
        arm64_wheel=None,
        arm64_wheel_sha256=None,
    )
    fingerprint = installer._install_fingerprint(
        role,
        "v5.0.0a6",
        {
            key: str(value) if isinstance(value, Path) else value
            for key, value in opts.items()
        },
    )
    installer._record_install(root, role, "v5.0.0a6", fingerprint)

    if platform == "win32":
        service = root / role / "service"
        service.mkdir(parents=True)
        launcher = service / "launcher.py"
        launcher.write_text("managed test")
        monkeypatch.setattr(
            switch_windows,
            "_native",
            lambda executable, query_role: {
                "state": "Running",
                "action": {
                    "execute": str(python.with_name("pythonw.exe")),
                    "arguments": subprocess.list2cmdline([str(launcher)]),
                    "working_directory": str(service),
                },
            },
        )
    else:
        unit = root / "systemd" / "user" / f"workgate-{role}.service"
        unit.parent.mkdir(parents=True)
        unit.write_text(f"# Managed by workgate {role} install-service")
        monkeypatch.setattr(
            switch,
            "_native",
            lambda query_role: {
                "LoadState": "loaded",
                "ActiveState": "active",
                "FragmentPath": str(unit),
                "ExecStart": f"{{ path={python} ; argv[]={python} ... ; }}",
            },
        )

    if role == "executor":

        def status(argv, **kw):
            assert argv[-2:] == ["--config", str(config)]
            return subprocess.CompletedProcess(
                argv,
                0,
                "State: running\nRuntime: current\n",
            )

        monkeypatch.setattr(installer.subprocess, "run", status)
    else:
        monkeypatch.setattr(
            installer.subprocess,
            "run",
            lambda *args, **kw: pytest.fail("no subprocess on no-op"),
        )

    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail("no uv sync/clone during no-op"),
    )
    monkeypatch.setattr(
        installer,
        "_write_launcher",
        lambda *a: pytest.fail("no launcher rewrite"),
    )
    if platform == "win32":
        monkeypatch.setattr(installer, "_check_launcher", lambda root: None)
    assert (
        installer.install(
            role=role,
            root=root,
            tag="v5.0.0a6",
            control_url=opts["control_url"],
            name=opts["name"],
            executor_config=opts["executor_config"],
        )
        == checkout
    )


def test_install_receipt_is_private_and_rejects_unknown_existing_data(
    installer, tmp_path
):
    root = tmp_path
    installer._record_install(root, "control", "v5.0.0a6", "a" * 64)
    content = (root / "install.json").read_text()
    assert "a" * 64 in content and "control" in content
    if os.name != "nt":
        assert (root / "install.json").stat().st_mode & 0o777 == 0o600
    (root / "install.json").write_text("unmanaged")
    with pytest.raises(RuntimeError, match="unknown install receipt"):
        installer._record_install(root, "control", "v5.0.0a6", "a" * 64)


def test_install_receipt_wrong_args_or_missing_record_never_triggers_noop(
    installer, tmp_path
):
    root = tmp_path
    good = installer._install_fingerprint(
        "executor", "v5.0.0a6", {"control_url": "https://a.example"}
    )
    bad = installer._install_fingerprint(
        "executor", "v5.0.0a6", {"control_url": "https://b.example"}
    )
    assert good != bad
    assert (
        installer._same_install(root, "executor", "v5.0.0a6", good, None, None)
        is None
    )
    installer._record_install(root, "executor", "v5.0.0a6", good)
    assert (
        installer._same_install(root, "executor", "v5.0.0a6", bad, None, None)
        is None
    )
    (root / "install.json").write_text("{broken")
    assert (
        installer._same_install(root, "executor", "v5.0.0a6", good, None, None)
        is None
    )


@pytest.mark.parametrize("bad", ["unmanaged", '{"x":1}', "{broken"])
def test_unknown_install_receipt_rejected_before_prepare(
    installer, monkeypatch, tmp_path, bad
):
    (tmp_path / "install.json").write_text(bad)
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail("must reject unknown receipt before sync"),
    )
    with pytest.raises(RuntimeError, match="unknown install receipt"):
        installer.install(role="control", tag="v5.0.0a6", root=tmp_path)


def test_symlinked_install_receipt_rejected_before_prepare(
    installer, monkeypatch, tmp_path
):
    target = tmp_path / "not-ours.json"
    target.write_text("do not touch")
    (tmp_path / "install.json").symlink_to(target)
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail("must reject symlinked receipt before sync"),
    )
    with pytest.raises(RuntimeError, match="symlinked install receipt"):
        installer.install(role="control", tag="v5.0.0a6", root=tmp_path)
    assert target.read_text() == "do not touch"


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize(
    "fault", ["stopped", "other-runtime", "unmanaged-unit"]
)
def test_installer_never_noops_for_unverified_native_binding(
    installer, monkeypatch, tmp_path, platform, fault
):
    switch = importlib.import_module("switch")
    windows = importlib.import_module("switch_windows")
    monkeypatch.setattr(installer.sys, "platform", platform)
    root = tmp_path
    version = root / "versions" / ("b" * 40)
    python = (
        version
        / ".venv"
        / ("Scripts/python.exe" if platform == "win32" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.touch()
    fingerprint = installer._install_fingerprint(
        "control", "v5.0.0a6", {"host": None}
    )
    installer._record_install(root, "control", "v5.0.0a6", fingerprint)
    monkeypatch.setattr(switch, "_staged_python", lambda *_: python)

    if platform == "win32":
        monkeypatch.setattr(windows, "_old_python", lambda *_: python)
        service = root / "control" / "service"
        service.mkdir(parents=True)
        launcher = service / "launcher.py"
        launcher.write_text("managed test")
        action = {
            "execute": str(
                python.with_name(
                    "different-pythonw.exe"
                    if fault == "other-runtime"
                    else "pythonw.exe"
                )
            ),
            "arguments": subprocess.list2cmdline([str(launcher)]),
            "working_directory": str(service),
        }
        if fault == "unmanaged-unit":
            action["working_directory"] = str(root / "foreign")
        monkeypatch.setattr(
            windows,
            "_native",
            lambda *_: {
                "state": "Ready" if fault == "stopped" else "Running",
                "action": action,
            },
        )
    else:
        monkeypatch.setattr(switch, "_old_python", lambda *_: python)
        unit = root / "systemd/user/workgate-control.service"
        unit.parent.mkdir(parents=True)
        unit.write_text(
            "foreign unit"
            if fault == "unmanaged-unit"
            else "# Managed by workgate control install-service"
        )
        monkeypatch.setattr(
            switch,
            "_native",
            lambda *_: {
                "LoadState": "loaded",
                "ActiveState": "inactive" if fault == "stopped" else "active",
                "FragmentPath": str(unit),
                "ExecStart": (
                    f"{{ path={root / 'foreign/python'} ; argv[]={python} ... ; }}"
                    if fault == "other-runtime"
                    else f"{{ path={python} ; argv[]={python} ... ; }}"
                ),
            },
        )

    assert (
        installer._same_install(
            root, "control", "v5.0.0a6", fingerprint, None, None
        )
        is None
    )


def test_existing_receipt_cannot_silently_change_install_role(
    installer, monkeypatch, tmp_path
):
    installer._record_install(tmp_path, "control", "v5.0.0a6", "a" * 64)
    monkeypatch.setattr(
        installer,
        "prepare",
        lambda **kw: pytest.fail("another role must reject before sync"),
    )
    with pytest.raises(RuntimeError, match="another role"):
        installer.install(
            role="executor",
            tag="v5.0.0a6",
            root=tmp_path,
            control_url="https://control.example",
        )
