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
    monkeypatch.setattr(installer, "_check_native_services", lambda *a: None)


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
